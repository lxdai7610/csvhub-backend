from contextlib import contextmanager, closing
from datetime import datetime, timezone
import json
import sqlite3
import uuid

from .config import Settings


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return uuid.uuid4().hex


def unpack(row):
    if row is None:
        return None
    item = dict(row)
    for key in ("columns", "options"):
        if key in item:
            item[key] = json.loads(item[key])
    return item


class Catalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare()
        self.path = settings.data_dir / "catalog.sqlite"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, columns TEXT NOT NULL,
                    active_version TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'uploading', error TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    upload_id TEXT NOT NULL REFERENCES uploads(id), options TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', phase TEXT NOT NULL DEFAULT 'importing',
                    processed INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
                    rejected INTEGER NOT NULL DEFAULT 0, bytes_read INTEGER NOT NULL DEFAULT 0,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_import ON jobs(dataset_id)
                    WHERE status IN ('queued','importing','indexing','verifying','warming','paused');
                CREATE INDEX IF NOT EXISTS job_queue ON jobs(status, created_at);
                CREATE TABLE IF NOT EXISTS dataset_uploads (
                    dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    upload_id TEXT NOT NULL REFERENCES uploads(id),
                    PRIMARY KEY(dataset_id,upload_id)
                );
                INSERT OR IGNORE INTO dataset_uploads SELECT dataset_id,upload_id FROM jobs;
                CREATE TABLE IF NOT EXISTS file_cleanup (
                    path TEXT PRIMARY KEY, object_type TEXT NOT NULL, object_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES datasets(id),
                    accepted INTEGER NOT NULL, rejected INTEGER NOT NULL,
                    file_bytes INTEGER NOT NULL, published_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, action TEXT NOT NULL,
                    object_id TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def get(self, table, item_id):
        assert table in ("datasets", "uploads", "jobs", "versions")
        with closing(self.connect()) as db:
            return unpack(db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone())

    def list(self, table, limit=100):
        assert table in ("datasets", "uploads", "jobs", "versions")
        with closing(self.connect()) as db:
            return [unpack(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))]

    def audit(self, db, action, item_id):
        db.execute("INSERT INTO audit(action,object_id,created_at) VALUES(?,?,?)", (action, item_id, now()))

    def create_dataset(self, body):
        item_id = identifier()
        with self.transaction() as db:
            db.execute("INSERT INTO datasets(id,name,columns,created_at) VALUES(?,?,?,?)",
                       (item_id, body.name, json.dumps([c.model_dump() for c in body.columns]), now()))
            self.audit(db, "dataset.create", item_id)
        return self.get("datasets", item_id)

    def create_upload(self, body):
        item_id = identifier()
        with self.transaction() as db:
            db.execute("INSERT INTO uploads(id,filename,size,sha256,created_at) VALUES(?,?,?,?,?)",
                       (item_id, body.filename, body.size, body.sha256.lower(), now()))
        return self.get("uploads", item_id)

    def create_job(self, dataset_id, body):
        item_id = identifier()
        options = body.model_dump(exclude={"upload_id"})
        with self.transaction() as db:
            db.execute("INSERT INTO jobs(id,dataset_id,upload_id,options,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                       (item_id, dataset_id, body.upload_id, json.dumps(options), now(), now()))
            db.execute("INSERT OR IGNORE INTO dataset_uploads VALUES(?,?)", (dataset_id, body.upload_id))
            self.audit(db, "import.create", item_id)
        return self.get("jobs", item_id)

    def update_job(self, item_id, **changes):
        allowed = {"status", "phase", "processed", "accepted", "rejected", "bytes_read",
                   "pause_requested", "cancel_requested", "error"}
        if not set(changes) <= allowed:
            raise ValueError("Unknown job attributes")
        changes["updated_at"] = now()
        with self.transaction() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                       (*changes.values(), item_id))

    def publish(self, job, accepted, rejected, file_bytes):
        # Version pointer and terminal job state change in the same catalog transaction.
        with self.transaction() as db:
            flags = db.execute("SELECT pause_requested,cancel_requested FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if flags["cancel_requested"] or flags["pause_requested"]:
                return False
            db.execute("INSERT OR IGNORE INTO versions VALUES(?,?,?,?,?,?)",
                       (job["id"], job["dataset_id"], accepted, rejected, file_bytes, now()))
            db.execute("UPDATE datasets SET active_version=? WHERE id=?", (job["id"], job["dataset_id"]))
            db.execute("UPDATE jobs SET status='completed',phase='completed',accepted=?,rejected=?,updated_at=? WHERE id=?",
                       (accepted, rejected, now(), job["id"]))
            self.audit(db, "version.publish", job["id"])
        return True

