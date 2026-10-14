"""Ordered, durable work batches on immutable published dataset versions."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import threading
import time
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .access import clean_text
from .catalog import identifier, now


def today_period():
    """Resolve each local midnight through the OS timezone, including DST changes."""
    current = datetime.now()
    today = current.date()
    start = datetime.combine(today, datetime.min.time()).astimezone(timezone.utc)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time()).astimezone(timezone.utc)
    offset = current.astimezone().strftime("%z")
    return {"today": today.isoformat(), "timezone": f"UTC{offset[:3]}:{offset[3:]}",
            "day_start": start.isoformat(timespec="seconds"), "day_end": end.isoformat(timespec="seconds")}


class ExtractionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(default=100, ge=100, le=1000, strict=True)
    request_id: UUID

    @field_validator("size")
    @classmethod
    def hundred_step(cls, value):
        if value % 100:
            raise ValueError("Extraction size must be a multiple of 100")
        return value


class AnnotationUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    marked: bool | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def note_text(cls, value):
        # Newlines and tabs are useful in notes; all other control/surrogate characters are rejected.
        if value is not None:
            clean_text(value.replace("\n", "").replace("\r", "").replace("\t", ""))
        return value


class Extractions:
    def __init__(self, catalog, settings):
        self.catalog, self.settings = catalog, settings
        self.slots = threading.BoundedSemaphore(settings.read_concurrency)
        with closing(catalog.connect()) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS extraction_progress (
                    user_id TEXT NOT NULL REFERENCES users(id), dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    version_id TEXT NOT NULL REFERENCES versions(id), last_row INTEGER NOT NULL,
                    extracted INTEGER NOT NULL, PRIMARY KEY(user_id,dataset_id,version_id)
                );
                CREATE TABLE IF NOT EXISTS extractions (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    dataset_id TEXT NOT NULL REFERENCES datasets(id), version_id TEXT NOT NULL REFERENCES versions(id),
                    request_id TEXT NOT NULL, requested_size INTEGER NOT NULL, count INTEGER NOT NULL,
                    first_row INTEGER NOT NULL, last_row INTEGER NOT NULL, has_more INTEGER NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(user_id,request_id)
                );
                CREATE INDEX IF NOT EXISTS extractions_owner ON extractions(user_id,dataset_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS extraction_items (
                    batch_id TEXT NOT NULL REFERENCES extractions(id), row_no INTEGER NOT NULL,
                    PRIMARY KEY(batch_id,row_no)
                );
                CREATE TABLE IF NOT EXISTS annotations (
                    user_id TEXT NOT NULL REFERENCES users(id), version_id TEXT NOT NULL REFERENCES versions(id),
                    row_no INTEGER NOT NULL, marked INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL, marked_at TEXT,
                    PRIMARY KEY(user_id,version_id,row_no)
                );
                CREATE INDEX IF NOT EXISTS annotations_version ON annotations(version_id);
            """)
        with catalog.transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(annotations)")}
            if "marked_at" not in columns:
                db.execute("ALTER TABLE annotations ADD COLUMN marked_at TEXT")
                # Old releases only recorded the latest annotation/notes update.
                db.execute("UPDATE annotations SET marked_at=updated_at WHERE marked=1")
            db.execute("CREATE INDEX IF NOT EXISTS extractions_statistics ON extractions(user_id,created_at,count)")
            db.execute("CREATE INDEX IF NOT EXISTS annotations_statistics ON annotations(marked,user_id,marked_at)")

    def dataset(self, dataset_id):
        dataset = self.catalog.get("datasets", dataset_id)
        if dataset is None:
            raise HTTPException(404, "Dataset not found")
        if not dataset["active_version"]:
            raise HTTPException(409, "Dataset has no published version")
        return dataset

    def records(self, version):
        # Only catalog-owned, published UUID versions may select a file.
        if len(version) != 32 or any(c not in "0123456789abcdef" for c in version):
            raise HTTPException(409, "Dataset version is unavailable")
        path = self.settings.data_dir / "versions" / f"{version}.sqlite"
        try:
            db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        except sqlite3.OperationalError:
            raise HTTPException(409, "Dataset version is unavailable")
        db.execute("PRAGMA query_only=ON")
        db.execute(f"PRAGMA cache_size=-{self.settings.sqlite_cache_kib}")
        db.execute("PRAGMA mmap_size=0")
        deadline = time.monotonic() + self.settings.query_timeout
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        return db

    def overview(self, user_id):
        period = today_period()
        bounds = (period["day_start"], period["day_end"])
        with closing(self.catalog.connect()) as db:
            db.execute("BEGIN")
            datasets = db.execute("SELECT COUNT(*),COALESCE(SUM(v.accepted),0) FROM datasets d "
                                  "JOIN versions v ON v.id=d.active_version").fetchone()
            batches = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(count),0),"
                "COALESCE(SUM(CASE WHEN created_at>=? AND created_at<? THEN count ELSE 0 END),0) "
                "FROM extractions WHERE user_id=?", (*bounds, user_id)).fetchone()
            marked = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(CASE WHEN marked_at>=? AND marked_at<? THEN 1 ELSE 0 END),0) "
                "FROM annotations WHERE user_id=? AND marked=1", (*bounds, user_id)).fetchone()
        return {"datasets": datasets[0], "records": datasets[1], "batches": batches[0],
                "extracted": batches[1], "marked": marked[0], "unmarked": batches[1] - marked[0],
                "today_extracted": batches[2], "today_marked": marked[1], **period}

    def user_statistics(self):
        period = today_period()
        bounds = (period["day_start"], period["day_end"])
        with closing(self.catalog.connect()) as db:
            # Aggregate independently so batch counts are not multiplied by annotation rows.
            rows = db.execute("""
                WITH extracted AS (
                    SELECT user_id, SUM(count) AS total,
                           SUM(CASE WHEN created_at>=? AND created_at<? THEN count ELSE 0 END) AS today
                    FROM extractions GROUP BY user_id
                ), marked AS (
                    SELECT user_id, COUNT(*) AS total,
                           SUM(CASE WHEN marked_at>=? AND marked_at<? THEN 1 ELSE 0 END) AS today
                    FROM annotations WHERE marked=1 GROUP BY user_id
                )
                SELECT u.id,u.username,u.role,u.enabled,
                       COALESCE(e.total,0) AS extracted,COALESCE(m.total,0) AS marked,
                       COALESCE(e.today,0) AS today_extracted,COALESCE(m.today,0) AS today_marked
                FROM users u LEFT JOIN extracted e ON e.user_id=u.id LEFT JOIN marked m ON m.user_id=u.id
                ORDER BY u.created_at,u.id
            """, (*bounds, *bounds)).fetchall()
        return {"items": [dict(row) for row in rows], **period}

    def status(self, user_id, dataset_id):
        dataset = self.dataset(dataset_id)
        with closing(self.catalog.connect()) as db:
            progress = db.execute("SELECT * FROM extraction_progress WHERE user_id=? AND dataset_id=? AND version_id=?",
                                  (user_id, dataset_id, dataset["active_version"])).fetchone()
            total = db.execute("SELECT accepted FROM versions WHERE id=?", (dataset["active_version"],)).fetchone()[0]
            batches = [dict(row) for row in db.execute("SELECT * FROM extractions WHERE user_id=? AND dataset_id=? "
                        "ORDER BY rowid DESC LIMIT 50", (user_id, dataset_id))]
        extracted = progress["extracted"] if progress else 0
        return {"version": dataset["active_version"], "total": total, "extracted": extracted,
                "remaining": total - extracted, "items": batches}

    def owned(self, db, user_id, batch_id):
        row = db.execute("SELECT * FROM extractions WHERE id=? AND user_id=?", (batch_id, user_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Extraction batch not found")
        return dict(row)

    def create(self, user_id, dataset_id, body):
        if not self.slots.acquire(timeout=self.settings.query_timeout):
            raise HTTPException(503, "Extraction capacity is busy; retry later")
        try:
            with self.catalog.transaction() as db:
                previous = db.execute("SELECT * FROM extractions WHERE user_id=? AND request_id=?",
                                      (user_id, str(body.request_id))).fetchone()
                if previous:
                    if previous["dataset_id"] != dataset_id or previous["requested_size"] != body.size:
                        raise HTTPException(409, "Extraction request identifier already used")
                    batch_id = previous["id"]
                else:
                    # Read pointer inside the allocation transaction; concurrent requests cannot overlap.
                    dataset = self.catalog.get("datasets", dataset_id)
                    if dataset is None:
                        raise HTTPException(404, "Dataset not found")
                    version = dataset["active_version"]
                    if version is None:
                        raise HTTPException(409, "Dataset has no published version")
                    progress = db.execute("SELECT * FROM extraction_progress WHERE user_id=? AND dataset_id=? AND version_id=?",
                                          (user_id, dataset_id, version)).fetchone()
                    last = progress["last_row"] if progress else 0
                    count = progress["extracted"] if progress else 0
                    rows, used, has_more = [], len(json.dumps(dataset["columns"], ensure_ascii=False).encode()) + 4096, False
                    with closing(self.records(version)) as records:
                        for row in records.execute("SELECT * FROM records WHERE row_no>? ORDER BY row_no LIMIT ?", (last, body.size + 1)):
                            values = {c["name"]: row[i + 1] for i, c in enumerate(dataset["columns"])}
                            cost = len(json.dumps(values, ensure_ascii=False).encode()) + 4096
                            if len(rows) >= body.size or used + cost > self.settings.max_response_bytes:
                                if not rows:
                                    raise HTTPException(422, "Record exceeds interactive response size limit")
                                has_more = True
                                break
                            rows.append(row[0])
                            used += cost
                    if not rows:
                        raise HTTPException(409, "All records have been extracted")
                    batch_id = identifier()
                    db.execute("INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (batch_id, user_id, dataset_id, version, str(body.request_id), body.size,
                                len(rows), rows[0], rows[-1], int(has_more), now()))
                    db.executemany("INSERT INTO extraction_items VALUES(?,?)", [(batch_id, row) for row in rows])
                    db.execute("INSERT INTO extraction_progress VALUES(?,?,?,?,?) ON CONFLICT(user_id,dataset_id,version_id) "
                               "DO UPDATE SET last_row=excluded.last_row,extracted=excluded.extracted",
                               (user_id, dataset_id, version, rows[-1], count + len(rows)))
                    self.catalog.audit(db, "extraction.create", batch_id)
            return self.get(user_id, batch_id, acquired=True)
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise HTTPException(503, "Extraction exceeded time budget; retry later") from error
            raise
        finally:
            self.slots.release()

    def get(self, user_id, batch_id, acquired=False):
        if not acquired and not self.slots.acquire(timeout=self.settings.query_timeout):
            raise HTTPException(503, "Extraction capacity is busy; retry later")
        try:
            with closing(self.catalog.connect()) as db:
                batch = self.owned(db, user_id, batch_id)
                dataset = self.catalog.get("datasets", batch["dataset_id"])
                annotations = {row["row_no"]: dict(row) for row in db.execute("SELECT a.* FROM annotations a "
                      "JOIN extraction_items i ON a.row_no=i.row_no WHERE i.batch_id=? AND a.user_id=? AND a.version_id=?",
                      (batch_id, user_id, batch["version_id"]))}
                membership = {row[0] for row in db.execute("SELECT row_no FROM extraction_items WHERE batch_id=?", (batch_id,))}
            results = []
            with closing(self.records(batch["version_id"])) as records:
                for row in records.execute("SELECT * FROM records WHERE row_no BETWEEN ? AND ? ORDER BY row_no",
                                           (batch["first_row"], batch["last_row"])):
                    if row[0] not in membership:
                        continue
                    mark = annotations.get(row[0], {})
                    results.append({"row_no": row[0], "values": {c["name"]: row[i + 1] for i, c in enumerate(dataset["columns"])},
                                    "marked": bool(mark.get("marked", 0)), "note": mark.get("note", ""),
                                    "revision": mark.get("revision", 0)})
            if len(results) != batch["count"]:
                raise HTTPException(409, "Dataset version is unavailable")
            return {"batch": batch, "columns": dataset["columns"], "rows": results}
        except sqlite3.OperationalError as error:
            if "interrupt" in str(error).lower():
                raise HTTPException(503, "Extraction exceeded time budget; retry later") from error
            raise
        finally:
            if not acquired:
                self.slots.release()

    def query_annotations(self, user_id, version_id, row_numbers):
        """Read only this page's annotations using the owner/version/row primary key."""
        if not row_numbers:
            return {}
        placeholders = ",".join("?" for _ in row_numbers)
        with closing(self.catalog.connect()) as db:
            rows = db.execute(
                "SELECT row_no,marked,note,revision,updated_at FROM annotations "
                f"WHERE user_id=? AND version_id=? AND row_no IN ({placeholders})",
                (user_id, version_id, *row_numbers)).fetchall()
        found = {row["row_no"]: {"marked": bool(row["marked"]), "note": row["note"],
                                "revision": row["revision"], "updated_at": row["updated_at"]} for row in rows}
        return {str(row): found.get(row, {"marked": False, "note": "", "revision": 0, "updated_at": None})
                for row in row_numbers}

    def annotate(self, user_id, batch_id, row_no, body):
        with self.catalog.transaction() as db:
            batch = self.owned(db, user_id, batch_id)
            if not db.execute("SELECT 1 FROM extraction_items WHERE batch_id=? AND row_no=?", (batch_id, row_no)).fetchone():
                raise HTTPException(404, "Extracted record not found")
            previous = db.execute("SELECT * FROM annotations WHERE user_id=? AND version_id=? AND row_no=?",
                                  (user_id, batch["version_id"], row_no)).fetchone()
            revision = previous["revision"] if previous else 0
            if body.revision != revision:
                raise HTTPException(409, "Annotation changed in another window; reload the batch")
            marked = body.marked if body.marked is not None else bool(previous["marked"] if previous else 0)
            note = body.note if body.note is not None else previous["note"] if previous else ""
            timestamp = now()
            marked_at = (previous["marked_at"] if previous and previous["marked"] else timestamp) if marked else None
            db.execute("INSERT INTO annotations(user_id,version_id,row_no,marked,note,revision,updated_at,marked_at) "
                       "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,version_id,row_no) "
                       "DO UPDATE SET marked=excluded.marked,note=excluded.note,revision=excluded.revision,"
                       "updated_at=excluded.updated_at,marked_at=excluded.marked_at",
                       (user_id, batch["version_id"], row_no, marked, note, revision + 1, timestamp, marked_at))
        return {"row_no": row_no, "marked": marked, "note": note, "revision": revision + 1}
