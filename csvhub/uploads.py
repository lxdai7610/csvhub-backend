from contextlib import closing
import hashlib
import os
import shutil

from .locking import file_lock


class UploadError(ValueError):
    pass


def upload_path(settings, upload_id, suffix=".csv"):
    # IDs always come from catalog rows, never from filename or a filesystem API parameter.
    return settings.data_dir / "uploads" / f"{upload_id}{suffix}"


def append_chunk(catalog, upload_id, offset, chunk_path, chunk_size):
    settings = catalog.settings
    with file_lock(upload_path(settings, upload_id, ".lock")):
        with catalog.transaction() as db:
            upload = db.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
            if upload is None:
                raise UploadError("Upload not found")
            if upload["state"] != "uploading":
                raise UploadError("Upload is not accepting chunks")
            if offset != upload["received"]:
                raise UploadError(f"Offset mismatch; expected {upload['received']}")
            if offset + chunk_size > upload["size"]:
                raise UploadError("Chunk exceeds declared file size")
            part = upload_path(settings, upload_id, ".part")
            with part.open("r+b" if part.exists() else "w+b") as output:
                output.seek(0, os.SEEK_END)
                if output.tell() < offset:
                    raise UploadError("Upload file is shorter than durable checkpoint")
                output.truncate(offset)  # Discard append left by a pre-commit crash.
                output.seek(offset)
                with chunk_path.open("rb") as source:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            db.execute("UPDATE uploads SET received=? WHERE id=?", (offset + chunk_size, upload_id))
    return catalog.get("uploads", upload_id)


def finish_upload(catalog, upload_id):
    settings = catalog.settings
    with file_lock(upload_path(settings, upload_id, ".lock")):
        upload = catalog.get("uploads", upload_id)
        if upload is None:
            raise UploadError("Upload not found")
        if upload["state"] == "ready":
            return upload
        if upload["received"] != upload["size"]:
            raise UploadError("File is incomplete")
        if upload["state"] not in ("uploading", "validating"):
            raise UploadError("Create a new upload after checksum failure")
        with catalog.transaction() as db:
            db.execute("UPDATE uploads SET state='validating' WHERE id=?", (upload_id,))
        final = upload_path(settings, upload_id)
        part = upload_path(settings, upload_id, ".part")
        source = final if final.exists() else part
        digest = hashlib.sha256()
        with source.open("rb") as file:
            remaining = upload["size"]
            while remaining:
                block = file.read(min(1024 * 1024, remaining))
                if not block:
                    raise UploadError("Upload file is shorter than declared size")
                digest.update(block)
                remaining -= len(block)
        if digest.hexdigest() != upload["sha256"]:
            with catalog.transaction() as db:
                db.execute("UPDATE uploads SET state='invalid',error='SHA256 mismatch' WHERE id=?", (upload_id,))
            raise UploadError("Whole-file SHA256 mismatch")
        if source == part:
            with part.open("r+b") as file:
                file.truncate(upload["size"])
                file.flush()
                os.fsync(file.fileno())
            part.replace(final)
        with catalog.transaction() as db:
            db.execute("UPDATE uploads SET state='ready',error=NULL WHERE id=?", (upload_id,))
            catalog.audit(db, "upload.complete", upload_id)
        return catalog.get("uploads", upload_id)

