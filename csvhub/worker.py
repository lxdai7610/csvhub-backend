"""One durable importer process; run with python -m csvhub.worker."""
from contextlib import closing
import csv
import json
import logging
import shutil
import signal
import sqlite3
import threading
import time

from .catalog import Catalog, unpack, now
from .config import Settings
from .locking import file_lock, LockBusy
from .parsing import normalize, CellError, RejectedRecord
from .tabular import open_table
from .query import QueryService, fingerprint
from .uploads import upload_path

LOG = logging.getLogger("csvhub.worker")


class JobStopped(Exception):
    def __init__(self, status, message=None):
        self.status, self.message = status, message


class Worker:
    def __init__(self, settings, catalog=None):
        self.settings = settings
        self.catalog = catalog or Catalog(settings)
        self.stop = threading.Event()
        self.last_control = 0
        self.interrupt_reason = None
        self.query_service = QueryService(settings, self.catalog)

    def control(self, job_id):
        if self.stopping():
            raise JobStopped("queued", "Worker stopped; task will resume on restart")
        job = self.catalog.get("jobs", job_id)
        if job["cancel_requested"]:
            raise JobStopped("cancelled")
        if job["pause_requested"]:
            raise JobStopped("paused")

    def stopping(self):
        if (self.settings.data_dir / "service-stop.request").exists():
            self.stop.set()
        return self.stop.is_set()

    def progress(self, job_id):
        if time.monotonic() - self.last_control < 0.5:
            return 0
        self.last_control = time.monotonic()
        try:
            self.control(job_id)
        except JobStopped as error:
            self.interrupt_reason = error
            return 1
        return 0

    def run_one(self):
        with self.catalog.transaction() as meta:
            row = meta.execute("""SELECT * FROM jobs WHERE status IN
                ('queued','importing','indexing','verifying','warming') ORDER BY created_at LIMIT 1""").fetchone()
            if row is None:
                return False
            job = unpack(row)
            meta.execute("UPDATE jobs SET status=phase,updated_at=? WHERE id=?", (now(), job["id"]))
        self.run_job(job)
        return True

    def run_job(self, job):
        # A terminal status can be committed just before the SQLite handle closes.
        # Deletion must also acquire this lock before removing a task's files.
        with file_lock(self.settings.data_dir / "locks" / f"{job['id']}.lock"):
            self._run_job(job)

    def _run_job(self, job):
        path = self.settings.data_dir / "versions" / f"{job['id']}.sqlite"
        dataset = self.catalog.get("datasets", job["dataset_id"])
        columns = dataset["columns"]
        db = None
        self.interrupt_reason = None
        try:
            self.control(job["id"])
            db = sqlite3.connect(path)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(f"PRAGMA cache_size=-{self.settings.sqlite_cache_kib}")
            db.execute("PRAGMA mmap_size=0")
            db.execute("PRAGMA temp_store=FILE")
            definitions = ",".join(f"c{i} {'INTEGER' if c['kind'] == 'integer' else 'TEXT'}"
                                   for i, c in enumerate(columns))
            db.execute(f"CREATE TABLE IF NOT EXISTS records(row_no INTEGER PRIMARY KEY,{definitions})")
            db.execute("""CREATE TABLE IF NOT EXISTS checkpoint(
                id INTEGER PRIMARY KEY CHECK(id=1), processed INTEGER NOT NULL,
                accepted INTEGER NOT NULL,rejected INTEGER NOT NULL,bytes_read INTEGER NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO checkpoint VALUES(1,0,0,0,0)")
            db.execute("CREATE TABLE IF NOT EXISTS errors(row_no INTEGER PRIMARY KEY,reason TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS warmup(id INTEGER PRIMARY KEY,payload TEXT NOT NULL)")
            db.commit()
            db.set_progress_handler(lambda: self.progress(job["id"]), 10000)
            self.sync_progress(db, job["id"])
            phase = job["phase"]
            if phase == "importing":
                self.import_rows(db, job, columns)
                phase = "indexing"
                self.catalog.update_job(job["id"], status=phase, phase=phase)
            if phase == "indexing":
                for i, column in enumerate(columns):
                    self.control(job["id"])
                    self.check_disk()
                    if column["indexed"]:
                        db.execute(f"CREATE INDEX IF NOT EXISTS ix_{i} ON records(c{i},row_no)")
                        db.commit()
                db.execute("PRAGMA optimize")
                db.commit()
                phase = "verifying"
                self.catalog.update_job(job["id"], status=phase, phase=phase)
            if phase == "verifying":
                self.control(job["id"])
                result = db.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise ValueError("Database integrity check failed")
                processed, accepted, rejected, _ = db.execute(
                    "SELECT processed,accepted,rejected,bytes_read FROM checkpoint WHERE id=1").fetchone()
                actual = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
                if actual != accepted or processed != accepted + rejected:
                    raise ValueError("Import checkpoint does not match row count")
                phase = "warming"
                self.catalog.update_job(job["id"], status=phase, phase=phase)
            if phase == "warming":
                self.control(job["id"])
                self.warm(db, job, columns)
                db.set_progress_handler(None, 0)
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
                accepted, rejected = db.execute("SELECT accepted,rejected FROM checkpoint WHERE id=1").fetchone()
                db.close()
                db = None
                if not self.catalog.publish(job, accepted, rejected, path.stat().st_size):
                    self.control(job["id"])
                LOG.info("Import %s completed: %s accepted, %s rejected", job["id"], accepted, rejected)
        except JobStopped as error:
            self.catalog.update_job(job["id"], status=error.status, error=error.message)
        except sqlite3.OperationalError as error:
            if self.interrupt_reason:
                stopped = self.interrupt_reason
                self.catalog.update_job(job["id"], status=stopped.status, error=stopped.message)
            elif "disk is full" in str(error).lower() or "database or disk is full" in str(error).lower():
                self.catalog.update_job(job["id"], status="paused", error="Disk is full; free space and resume")
            else:
                self.catalog.update_job(job["id"], status="failed", error=f"SQLite error: {error}")
        except (ValueError, UnicodeError, csv.Error, OSError) as error:
            # Never log source cell contents or return arbitrary file paths from OS exceptions.
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else type(error).__name__
            self.catalog.update_job(job["id"], status="failed", error=message[:500])
        except Exception as error:
            self.catalog.update_job(job["id"], status="failed", error=f"Internal importer error: {type(error).__name__}")
        finally:
            if db is not None:
                db.set_progress_handler(None, 0)
                db.close()  # Uncommitted rows and incomplete index creation roll back.

    def check_disk(self):
        if shutil.disk_usage(self.settings.data_dir).free < self.settings.min_free_bytes:
            raise JobStopped("paused", "Low disk space; free space and resume")

    def sync_progress(self, db, job_id):
        processed, accepted, rejected, read = db.execute(
            "SELECT processed,accepted,rejected,bytes_read FROM checkpoint WHERE id=1").fetchone()
        self.catalog.update_job(job_id, processed=processed, accepted=accepted, rejected=rejected, bytes_read=read)

    def import_rows(self, db, job, columns):
        checkpoint = db.execute("SELECT processed,accepted,rejected,bytes_read FROM checkpoint WHERE id=1").fetchone()
        previous, accepted, rejected, _ = checkpoint
        upload = self.catalog.get("uploads", job["upload_id"])
        source_path = upload_path(self.settings, job["upload_id"])
        if not source_path.exists() or source_path.stat().st_size != upload["size"]:
            raise ValueError("Completed upload is missing or changed")
        options = job["options"]
        placeholders = ",".join("?" for _ in range(len(columns) + 1))
        insert_sql = f"INSERT INTO records VALUES({placeholders})"
        batch, errors, batch_bytes, since_commit, ordinal = [], [], 0, 0, 0

        def commit_batch(read_bytes):
            nonlocal batch, errors, batch_bytes, since_commit
            self.control(job["id"])
            self.check_disk()
            with db:
                db.executemany(insert_sql, batch)
                # Keep only the first 100 error summaries, without source values.
                remaining = max(0, 100 - db.execute("SELECT COUNT(*) FROM errors").fetchone()[0])
                db.executemany("INSERT OR IGNORE INTO errors VALUES(?,?)", errors[:remaining])
                db.execute("UPDATE checkpoint SET processed=?,accepted=?,rejected=?,bytes_read=? WHERE id=1",
                           (ordinal, accepted, rejected, read_bytes))
            self.sync_progress(db, job["id"])
            batch, errors, batch_bytes, since_commit = [], [], 0, 0

        with open_table(source_path, upload["filename"], options, self.settings) as source:
            header = source.header
            if not header:
                raise ValueError("Source has no header")
            if len(header) != len(set(header)):
                raise ValueError("Source header contains duplicate names")
            if any(column["source"] not in header for column in columns):
                raise ValueError("Mapped source columns are missing from the source header")
            source_indexes = [header.index(column["source"]) for column in columns]
            for row in source:
                ordinal += 1
                if ordinal <= previous:
                    if ordinal % self.settings.batch_rows == 0:
                        self.control(job["id"])
                    continue  # Replay parsing only; durable records are never inserted twice.
                since_commit += 1
                batch_bytes += source.record_bytes
                reason, values = None, []
                if isinstance(row, RejectedRecord):
                    reason = str(row.error)
                    if options["on_error"] == "fail":
                        raise row.error
                elif len(row) != len(header):
                    reason = "Column count differs from header"
                else:
                    for column, source_index in zip(columns, source_indexes):
                        try:
                            values.append(normalize(row[source_index], column))
                        except (ValueError, OverflowError):
                            reason = f"Invalid mapped field: {column['name']} ({column['kind']})"
                            if isinstance(row[source_index], CellError):
                                reason += ": " + str(row[source_index])
                            break
                if reason:
                    if options["on_error"] == "fail":
                        raise ValueError(f"Record {ordinal}: {reason}")
                    rejected += 1
                    if len(errors) < 100:
                        errors.append((ordinal, reason))
                else:
                    accepted += 1
                    batch.append((ordinal, *values))
                if since_commit >= self.settings.batch_rows or batch_bytes >= self.settings.batch_bytes:
                    commit_batch(source.read_bytes)
            if ordinal < previous:
                raise ValueError("Source is shorter than committed checkpoint")
            if since_commit or previous == 0:
                commit_batch(upload["size"])
            elif ordinal == previous:
                self.sync_progress(db, job["id"])

    def warm(self, db, job, columns):
        rows, used, more = [], 0, False
        for row in db.execute("SELECT * FROM records ORDER BY row_no LIMIT 51"):
            size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if len(rows) >= 50 or used + size > self.settings.max_response_bytes:
                more = True
                break
            rows.append({"_row": row[0], **{c["name"]: row[i + 1] for i, c in enumerate(columns)}})
            used += size
        cursor = None
        if more and rows:
            cursor = self.query_service._encode_cursor({"version": job["id"], "query": fingerprint(job["dataset_id"], []),
                                                       "row": rows[-1]["_row"], "value": None})
        response = {"version": job["id"], "rows": rows, "next_cursor": cursor, "cache_hit": False}
        with db:
            if not rows and more:
                db.execute("DELETE FROM warmup")
            else:
                db.execute("INSERT OR REPLACE INTO warmup VALUES(1,?)", (json.dumps(response, ensure_ascii=True),))

    def serve(self):
        with file_lock(self.settings.data_dir / "worker.lock"):
            temporary_directory = self.settings.sqlite_temp_dir
            if temporary_directory:
                from pathlib import Path
                scratch = Path(temporary_directory).resolve()
                scratch.mkdir(parents=True, exist_ok=True)
                # SQLite's Windows scratch override is global. Set once, in this single
                # worker process, before any sort is run; never change it during jobs.
                with closing(sqlite3.connect(":memory:")) as setup:
                    setup.execute("PRAGMA temp_store_directory='" + str(scratch).replace("'", "''") + "'")
            LOG.info("Importer ready")
            while not self.stopping():
                if not self.run_one():
                    self.stop.wait(0.5)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    worker = Worker(Settings.from_env())
    signal.signal(signal.SIGINT, lambda *_: worker.stop.set())
    signal.signal(signal.SIGTERM, lambda *_: worker.stop.set())
    try:
        worker.serve()
    except LockBusy:
        raise SystemExit("Only one importer may use this data directory")


if __name__ == "__main__":
    main()
