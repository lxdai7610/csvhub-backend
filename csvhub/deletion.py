"""Catalog-owned deletion, with recoverable file staging and durable cleanup."""
from contextlib import contextmanager, closing, ExitStack
import logging
import json
import os
from pathlib import Path
import re
import threading

from .catalog import identifier, now
from .locking import file_lock

LOG = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled"}
UUID = re.compile(r"[0-9a-f]{32}\Z")


class DeletionError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


class DatasetAccess:
    """Readers run concurrently; deletion refuses an in-flight read or deletion."""
    def __init__(self):
        self.lock = threading.Lock()
        self.states = {}

    @contextmanager
    def hold(self, dataset_id, deleting=False):
        with self.lock:
            readers, writer = self.states.get(dataset_id, (0, False))
            if writer or (deleting and readers):
                raise DeletionError(409, "Dataset is in use; retry shortly")
            self.states[dataset_id] = (readers + (not deleting), deleting)
        try:
            yield
        finally:
            with self.lock:
                readers, _ = self.states[dataset_id]
                if deleting or readers == 1:
                    self.states.pop(dataset_id)
                else:
                    self.states[dataset_id] = (readers - 1, False)


def owned_path(root, folder, item_id, suffix):
    if not UUID.fullmatch(item_id):
        raise DeletionError(409, "Invalid stored resource identifier")
    directory = root / folder
    path = directory / f"{item_id}{suffix}"
    # Never follow a catalog path or symlink outside the managed data directory.
    if directory.resolve() != directory or path.is_symlink() or path.resolve().parent != directory:
        raise DeletionError(409, "Local file path is not safe to delete")
    return path


class FileStage:
    def __init__(self, root):
        self.root = root
        self.directory = root / "deletions" / identifier()
        self.moves = []
        self.committed = False
        self.bytes = 0

    def save_manifest(self):
        temporary = self.directory / "manifest.tmp"
        manifest = [{"source": str(source.relative_to(self.root)), "target": target.name} for source, target in self.moves]
        with temporary.open("w", encoding="utf-8") as file:
            json.dump(manifest, file)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.directory / "manifest.json")

    def __enter__(self):
        return self

    def add(self, paths, db, kind, object_id):
        for source in paths:
            if not source.exists():
                continue
            if not source.is_file():
                raise DeletionError(409, "Local file path is not safe to delete")
            if (self.root / "deletions").resolve() != self.root / "deletions":
                raise DeletionError(409, "Local file path is not safe to delete")
            self.directory.mkdir(exist_ok=True)
            if self.directory.resolve() != self.directory:
                raise DeletionError(409, "Local file path is not safe to delete")
            target = self.directory / source.name
            size = source.stat().st_size
            self.moves.append((source, target))
            self.save_manifest()
            source.rename(target)
            self.bytes += size
            db.execute("INSERT INTO file_cleanup VALUES(?,?,?,?)",
                       (str(target.relative_to(self.root)), kind, object_id, now()))

    def __exit__(self, error_type, error, traceback):
        if self.directory.resolve() != self.directory:
            raise DeletionError(409, "Local file path is not safe to delete")
        if not self.committed:
            for source, target in reversed(self.moves):
                if target.exists():
                    if source.exists():
                        raise RuntimeError("Cannot restore staged file over an existing file")
                    target.rename(source)
            if self.directory.exists():
                (self.directory / "manifest.json").unlink(missing_ok=True)
                (self.directory / "manifest.tmp").unlink(missing_ok=True)
                self.directory.rmdir()
        elif self.directory.exists():
            try:
                (self.directory / "manifest.json").unlink(missing_ok=True)
            except OSError:
                pass


class Deletions:
    def __init__(self, catalog, cache):
        self.catalog, self.cache = catalog, cache
        self.root = catalog.settings.data_dir.resolve()
        self.access = DatasetAccess()

    def recover_staging(self):
        """Restore a rename whose catalog transaction did not survive a crash."""
        directory = self.root / "deletions"
        if directory.resolve() != directory:
            raise DeletionError(409, "Local file path is not safe to delete")
        with closing(self.catalog.connect()) as db:
            pending = {row[0] for row in db.execute("SELECT path FROM file_cleanup")}
        for folder in directory.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not UUID.fullmatch(folder.name):
                continue
            manifest = folder / "manifest.json"
            if not manifest.exists() or manifest.is_symlink():
                continue
            moves = json.loads(manifest.read_text(encoding="utf-8"))
            for move in moves:
                relative = Path(move["source"])
                if len(relative.parts) != 2 or relative.parts[0] not in ("uploads", "versions") or move["target"] != relative.name:
                    raise DeletionError(409, "Local file path is not safe to delete")
                item_id, suffix = relative.name[:32], relative.name[32:]
                allowed = (".csv", ".part") if relative.parts[0] == "uploads" else (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")
                if suffix not in allowed:
                    raise DeletionError(409, "Local file path is not safe to delete")
                source = owned_path(self.root, relative.parts[0], item_id, suffix)
                target = folder / relative.name
                if target.is_symlink():
                    raise DeletionError(409, "Local file path is not safe to delete")
                if target.exists() and str(target.relative_to(self.root)) not in pending:
                    if source.exists():
                        raise DeletionError(409, "Cannot restore a local file over an existing file")
                    target.rename(source)
            manifest.unlink()
            (folder / "manifest.tmp").unlink(missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass

    def version_paths(self, ids):
        return [owned_path(self.root, "versions", item_id, suffix) for item_id in set(ids)
                for suffix in (".sqlite", ".sqlite-wal", ".sqlite-shm", ".sqlite-journal")]

    def upload_paths(self, upload_id):
        return [owned_path(self.root, "uploads", upload_id, suffix) for suffix in (".csv", ".part")]

    def purge_pending(self, kind=None, object_id=None):
        with closing(self.catalog.connect()) as db:
            sql = "SELECT * FROM file_cleanup"
            rows = db.execute(sql + " WHERE object_type=? AND object_id=?", (kind, object_id)).fetchall() if kind else db.execute(sql).fetchall()
        for row in rows:
            relative = Path(row["path"])
            path = self.root / relative
            if (relative.is_absolute() or len(relative.parts) != 3 or relative.parts[0] != "deletions"
                    or not UUID.fullmatch(relative.parts[1]) or path.is_symlink()
                    or path.parent.resolve() != path.parent
                    or not path.resolve().is_relative_to(self.root / "deletions")):
                LOG.error("Refusing invalid cleanup path")
                continue
            try:
                path.unlink(missing_ok=True)
                with self.catalog.transaction() as db:
                    db.execute("DELETE FROM file_cleanup WHERE path=?", (row["path"],))
                try:
                    path.parent.rmdir()
                except OSError:
                    pass
            except OSError:
                # Retry at startup and periodically. Do not claim the bytes are gone.
                LOG.warning("Local file cleanup deferred")
        with closing(self.catalog.connect()) as db:
            if kind:
                return db.execute("SELECT COUNT(*) FROM file_cleanup WHERE object_type=? AND object_id=?", (kind, object_id)).fetchone()[0]
            return db.execute("SELECT COUNT(*) FROM file_cleanup").fetchone()[0]

    def pending(self, kind, object_id):
        with closing(self.catalog.connect()) as db:
            return db.execute("SELECT 1 FROM file_cleanup WHERE object_type=? AND object_id=?", (kind, object_id)).fetchone() is not None

    def unused_upload(self, db, upload_id, locks, paths):
        if (db.execute("SELECT 1 FROM jobs WHERE upload_id=? LIMIT 1", (upload_id,)).fetchone()
                or db.execute("SELECT 1 FROM dataset_uploads WHERE upload_id=? LIMIT 1", (upload_id,)).fetchone()):
            return False
        lock = owned_path(self.root, "uploads", upload_id, ".lock")
        locks.enter_context(file_lock(lock))
        paths.extend(self.upload_paths(upload_id))
        db.execute("DELETE FROM uploads WHERE id=?", (upload_id,))
        return True

    def delete_job(self, job_id):
        job = self.catalog.get("jobs", job_id)
        if job is None:
            if self.pending("job", job_id):
                return {"status": "deleted", "cleanup_pending": self.purge_pending("job", job_id)}
            raise DeletionError(404, "Job not found")
        try:
            with self.access.hold(job["dataset_id"], deleting=True), ExitStack() as locks, FileStage(self.root) as stage:
                with self.catalog.transaction() as db:
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
                    if row is None:
                        raise DeletionError(404, "Job not found")
                    if row["status"] not in TERMINAL:
                        raise DeletionError(409, "Only completed, failed or cancelled tasks can be deleted")
                    locks.enter_context(file_lock(owned_path(self.root, "locks", job_id, ".lock")))
                    published = db.execute("SELECT 1 FROM versions WHERE id=?", (job_id,)).fetchone() is not None
                    paths = [] if published else self.version_paths([job_id])
                    upload_id = row["upload_id"]
                    db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
                    # The same raw upload may back retries or other datasets.
                    if not db.execute("SELECT 1 FROM jobs WHERE dataset_id=? AND upload_id=? LIMIT 1",
                                      (row["dataset_id"], upload_id)).fetchone():
                        db.execute("DELETE FROM dataset_uploads WHERE dataset_id=? AND upload_id=?", (row["dataset_id"], upload_id))
                    removed_upload = self.unused_upload(db, upload_id, locks, paths)
                    stage.add(paths, db, "job", job_id)
                    self.catalog.audit(db, "import.delete", job_id)
                stage.committed = True
        except OSError as error:
            raise DeletionError(409, "Local files are in use; deletion was not completed") from error
        return {"status": "deleted", "published_data_preserved": published, "retained_uploads": int(not removed_upload),
                "staged_bytes": stage.bytes, "cleanup_pending": self.purge_pending("job", job_id)}

    def delete_dataset(self, dataset_id):
        if self.catalog.get("datasets", dataset_id) is None:
            if self.pending("dataset", dataset_id):
                return {"status": "deleted", "cleanup_pending": self.purge_pending("dataset", dataset_id)}
            raise DeletionError(404, "Dataset not found")
        try:
            with self.access.hold(dataset_id, deleting=True), ExitStack() as locks, FileStage(self.root) as stage:
                with self.catalog.transaction() as db:
                    if not db.execute("SELECT 1 FROM datasets WHERE id=?", (dataset_id,)).fetchone():
                        raise DeletionError(404, "Dataset not found")
                    jobs = db.execute("SELECT id,status,upload_id FROM jobs WHERE dataset_id=?", (dataset_id,)).fetchall()
                    if any(job["status"] not in TERMINAL for job in jobs):
                        raise DeletionError(409, "Cancel or finish all imports before deleting the dataset")
                    for job in jobs:
                        locks.enter_context(file_lock(owned_path(self.root, "locks", job["id"], ".lock")))
                    versions = [row[0] for row in db.execute("SELECT id FROM versions WHERE dataset_id=?", (dataset_id,))]
                    uploads = {row[0] for row in db.execute("SELECT upload_id FROM dataset_uploads WHERE dataset_id=?", (dataset_id,))}
                    uploads.update(job["upload_id"] for job in jobs)
                    paths = self.version_paths(versions + [job["id"] for job in jobs])
                    db.execute("DELETE FROM extraction_items WHERE batch_id IN (SELECT id FROM extractions WHERE dataset_id=?)", (dataset_id,))
                    db.execute("DELETE FROM extractions WHERE dataset_id=?", (dataset_id,))
                    db.execute("DELETE FROM extraction_progress WHERE dataset_id=?", (dataset_id,))
                    db.execute("DELETE FROM annotations WHERE version_id IN (SELECT id FROM versions WHERE dataset_id=?)", (dataset_id,))
                    db.execute("DELETE FROM jobs WHERE dataset_id=?", (dataset_id,))
                    db.execute("DELETE FROM versions WHERE dataset_id=?", (dataset_id,))
                    db.execute("DELETE FROM dataset_uploads WHERE dataset_id=?", (dataset_id,))
                    retained = sum(not self.unused_upload(db, upload_id, locks, paths) for upload_id in sorted(uploads))
                    db.execute("DELETE FROM datasets WHERE id=?", (dataset_id,))
                    stage.add(paths, db, "dataset", dataset_id)
                    self.catalog.audit(db, "dataset.delete", dataset_id)
                stage.committed = True
                self.cache.invalidate_versions(versions)
        except OSError as error:
            raise DeletionError(409, "Local files are in use; deletion was not completed") from error
        return {"status": "deleted", "retained_uploads": retained, "staged_bytes": stage.bytes,
                "cleanup_pending": self.purge_pending("dataset", dataset_id)}
