from contextlib import asynccontextmanager, closing
import csv
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query as QueryParam, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import __version__
from .catalog import Catalog, identifier, now
from .config import Settings
from .locking import LockBusy, file_lock
from .deletion import Deletions, DeletionError
from .models import DatasetCreate, UploadCreate, ImportCreate, Query
from .parsing import CSVParseError, RejectedRecord
from .middleware import BodyLimitMiddleware
from .parsing import CellError
from .tabular import open_table, source_format, TableError
from .query import QueryError, QueryTimeout, QueryService
from .uploads import UploadError, append_chunk, finish_upload, upload_path
from .access import Accounts, AccountInput, AccountUpdate, Principal
from .extraction import Extractions, ExtractionCreate, AnnotationUpdate


def create_app(settings=None):
    settings = settings or Settings.from_env()
    catalog = Catalog(settings)
    service = QueryService(settings, catalog)
    token = settings.token()
    accounts = Accounts(catalog, token)
    extractions = Extractions(catalog, settings)
    deletions = Deletions(catalog, service.cache)
    excel_preview = threading.BoundedSemaphore(1)

    @asynccontextmanager
    async def lifespan(app):
        stop_cleanup = threading.Event()
        await run_in_threadpool(deletions.recover_staging)
        await run_in_threadpool(deletions.purge_pending)

        def cleanup_files():
            while not stop_cleanup.wait(30):
                try:
                    deletions.purge_pending()
                except (OSError, sqlite3.Error):
                    pass

        cleanup_thread = threading.Thread(target=cleanup_files, daemon=True)
        cleanup_thread.start()
        try:
            yield
        finally:
            stop_cleanup.set()
            cleanup_thread.join(timeout=1)

    app = FastAPI(title="CSVHub Backend", version=__version__, lifespan=lifespan,
                  description="Generic CSV/Excel datasets. Streaming uploads, durable import jobs and indexed SQLite queries.")
    app.state.catalog, app.state.queries = catalog, service
    app.state.accounts, app.state.extractions = accounts, extractions
    app.state.deletions = deletions
    app.add_middleware(BodyLimitMiddleware, chunk_limit=settings.max_chunk_bytes)
    web_dir = Path(__file__).with_name("web")
    app.mount("/assets", StaticFiles(directory=web_dir), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(web_dir / "login.html", headers={"Cache-Control": "no-store"})

    @app.get("/workspace", include_in_schema=False)
    def workspace():
        return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-store"})

    def authorize(authorization: Annotated[str | None, Header()] = None):
        return accounts.authenticate(authorization)

    def require_admin(user: Principal = Depends(authorize)):
        if user.role != "admin":
            raise HTTPException(403, "Administrator permission required")
        return user

    @app.exception_handler(RequestValidationError)
    async def validation_error(_, error):
        # Validation responses never echo credentials, notes or submitted source values.
        return JSONResponse(status_code=422, content={"detail": [
            {"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in error.errors()]})

    @app.post("/api/v1/auth/login", tags=["accounts"])
    def login(body: AccountInput, request: Request):
        return accounts.login(body, request.client.host if request.client else "local")

    def require(table, item_id):
        result = catalog.get(table, item_id)
        if result is None:
            raise HTTPException(404, f"{table[:-1].capitalize()} not found")
        return result

    @app.exception_handler(QueryError)
    async def query_error(_, error):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @app.exception_handler(QueryTimeout)
    async def timeout_error(_, error):
        return JSONResponse(status_code=503, content={"detail": str(error)}, headers={"Retry-After": "1"})

    @app.exception_handler(UploadError)
    async def upload_error(_, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(LockBusy)
    async def lock_error(_, error):
        return JSONResponse(status_code=409, content={"detail": str(error)}, headers={"Retry-After": "1"})

    @app.exception_handler(DeletionError)
    async def deletion_error(_, error):
        return JSONResponse(status_code=error.status, content={"detail": error.detail})

    @app.get("/health", tags=["service"])
    def health():
        return {"status": "ok", "version": __version__}

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])

    @router.get("/auth/me", tags=["accounts"])
    def current_user(user: Principal = Depends(authorize)):
        return user.public()

    @router.post("/auth/logout", tags=["accounts"])
    def logout(user: Principal = Depends(authorize)):
        if user.token_digest:
            with catalog.transaction() as db:
                db.execute("DELETE FROM sessions WHERE digest=?", (user.token_digest,))
        return {"status": "ok"}

    @router.get("/users", dependencies=[Depends(require_admin)], tags=["accounts"])
    def list_users(limit: int = QueryParam(200, ge=1, le=1000)):
        return {"items": accounts.list_users(limit)}

    @router.post("/users", dependencies=[Depends(require_admin)], status_code=201, tags=["accounts"])
    def create_user(body: AccountInput):
        return accounts.create(body)

    @router.patch("/users/{user_id}", dependencies=[Depends(require_admin)], tags=["accounts"])
    def update_user(user_id: str, body: AccountUpdate):
        return accounts.update(user_id, body)

    @router.get("/overview", tags=["extractions"])
    def overview(user: Principal = Depends(authorize)):
        return extractions.overview(user.id)

    @router.get("/overview/users", dependencies=[Depends(require_admin)], tags=["extractions"])
    def user_statistics():
        return extractions.user_statistics()

    @router.get("/datasets/{dataset_id}/extractions", tags=["extractions"])
    def extraction_status(dataset_id: str, user: Principal = Depends(authorize)):
        with deletions.access.hold(dataset_id):
            return extractions.status(user.id, dataset_id)

    @router.post("/datasets/{dataset_id}/extractions", status_code=201, tags=["extractions"])
    def extract(dataset_id: str, body: ExtractionCreate, user: Principal = Depends(authorize)):
        with deletions.access.hold(dataset_id):
            return extractions.create(user.id, dataset_id, body)

    def batch_dataset(user, batch_id):
        with closing(catalog.connect()) as db:
            return extractions.owned(db, user.id, batch_id)["dataset_id"]

    @router.get("/extractions/{batch_id}", tags=["extractions"])
    def get_extraction(batch_id: str, user: Principal = Depends(authorize)):
        with deletions.access.hold(batch_dataset(user, batch_id)):
            return extractions.get(user.id, batch_id)

    @router.patch("/extractions/{batch_id}/rows/{row_no}", tags=["extractions"])
    def annotation(batch_id: str, row_no: int, body: AnnotationUpdate, user: Principal = Depends(authorize)):
        with deletions.access.hold(batch_dataset(user, batch_id)):
            return extractions.annotate(user.id, batch_id, row_no, body)

    @router.post("/datasets", status_code=201, tags=["datasets"], dependencies=[Depends(require_admin)])
    def create_dataset(body: DatasetCreate):
        return catalog.create_dataset(body)

    @router.get("/datasets", tags=["datasets"])
    def list_datasets(limit: int = QueryParam(100, ge=1, le=200)):
        return {"items": catalog.list("datasets", limit)}

    @router.get("/datasets/{dataset_id}", tags=["datasets"])
    def get_dataset(dataset_id: str):
        result = require("datasets", dataset_id)
        result["version"] = catalog.get("versions", result["active_version"]) if result["active_version"] else None
        return result

    @router.delete("/datasets/{dataset_id}", tags=["datasets"], dependencies=[Depends(require_admin)])
    def delete_dataset(dataset_id: str):
        return deletions.delete_dataset(dataset_id)

    @router.get("/datasets/{dataset_id}/versions", tags=["datasets"], dependencies=[Depends(require_admin)])
    def list_versions(dataset_id: str, limit: int = QueryParam(100, ge=1, le=200)):
        require("datasets", dataset_id)
        with closing(catalog.connect()) as db:
            rows = db.execute("SELECT * FROM versions WHERE dataset_id=? ORDER BY published_at DESC LIMIT ?",
                              (dataset_id, limit)).fetchall()
        return {"items": [dict(row) for row in rows]}

    @router.post("/uploads", status_code=201, tags=["uploads"], dependencies=[Depends(require_admin)])
    def create_upload(body: UploadCreate):
        try:
            format = source_format(body.filename)
        except TableError as error:
            raise HTTPException(422, str(error))
        if body.size > settings.max_upload_bytes:
            raise HTTPException(413, "Upload exceeds configured file size limit")
        if format == "xls" and body.size > settings.max_xls_bytes:
            raise HTTPException(413, "Legacy XLS exceeds configured size limit; use XLSX or CSV")
        if shutil.disk_usage(settings.data_dir).free < body.size + settings.min_free_bytes:
            raise HTTPException(507, "Not enough disk space for upload")
        # Filename is display-only, never used to construct paths.
        return catalog.create_upload(body)

    @router.get("/uploads/{upload_id}", tags=["uploads"], dependencies=[Depends(require_admin)])
    def get_upload(upload_id: str):
        return require("uploads", upload_id)

    @router.put("/uploads/{upload_id}/chunks", tags=["uploads"], dependencies=[Depends(require_admin)])
    async def put_chunk(upload_id: str, request: Request, offset: int = QueryParam(ge=0),
                        x_chunk_sha256: Annotated[str, Header()] = ""):
        require("uploads", upload_id)
        if len(x_chunk_sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in x_chunk_sha256):
            raise HTTPException(422, "X-Chunk-SHA256 must be a 64-character hexadecimal digest")
        temporary = settings.data_dir / "uploads" / f"{identifier()}.chunk"
        total, digest = 0, hashlib.sha256()
        try:
            # The network body is streamed to disk; chunks are not buffered in full by the API.
            with temporary.open("xb") as file:
                async for block in request.stream():
                    total += len(block)
                    if total > settings.max_chunk_bytes:
                        raise HTTPException(413, "Chunk exceeds configured chunk size limit")
                    digest.update(block)
                    await run_in_threadpool(file.write, block)
                await run_in_threadpool(file.flush)
            if total == 0:
                raise HTTPException(422, "Chunk must not be empty")
            if digest.hexdigest() != x_chunk_sha256.lower():
                raise HTTPException(422, "Chunk SHA256 mismatch")
            return await run_in_threadpool(append_chunk, catalog, upload_id, offset, temporary, total)
        finally:
            temporary.unlink(missing_ok=True)

    @router.post("/uploads/{upload_id}/complete", tags=["uploads"], dependencies=[Depends(require_admin)])
    def complete_upload(upload_id: str):
        require("uploads", upload_id)
        return finish_upload(catalog, upload_id)

    @router.get("/uploads/{upload_id}/preview", tags=["uploads"], dependencies=[Depends(require_admin)])
    def preview(upload_id: str, encoding: str = "utf-8-sig", delimiter: str = ",",
                limit: int = QueryParam(5, ge=1, le=20), sheet: str | None = QueryParam(None, min_length=1, max_length=200),
                row_mode: Literal["standard", "single_line"] = "standard"):
        upload = require("uploads", upload_id)
        if upload["state"] != "ready":
            raise HTTPException(409, "Complete the upload before preview")
        # Reuse model validation for permitted encodings and delimiter.
        try:
            options = ImportCreate(upload_id=upload_id, encoding=encoding, delimiter=delimiter, sheet=sheet,
                                   row_mode=row_mode)
        except ValueError:
            raise HTTPException(422, "Unsupported encoding or delimiter")
        is_excel = source_format(upload["filename"]) != "csv"
        acquired = excel_preview.acquire(blocking=False) if is_excel else False
        if is_excel and not acquired:
            raise HTTPException(429, "An Excel preview is already running; retry shortly")
        try:
            with file_lock(upload_path(settings, upload_id, ".lock")):
                upload = require("uploads", upload_id)
                return read_preview(upload, options, limit)
        except TableError as error:
            raise HTTPException(422, str(error))
        except CSVParseError as error:
            raise HTTPException(422, str(error))
        except (ValueError, UnicodeError, csv.Error):
            raise HTTPException(422, "CSV parsing failed; check format, encoding and record size")
        finally:
            if acquired:
                excel_preview.release()

    def read_preview(upload, options, limit):
        with open_table(upload_path(settings, upload["id"]), upload["filename"], options.model_dump(), settings) as source:
            rows, used, warnings, scanned, rejected = [], 0, set(), 0, 0
            for row in source:
                scanned += 1
                used += source.record_bytes
                if used > settings.max_response_bytes:
                    break
                if isinstance(row, RejectedRecord):
                    rejected += 1
                    warnings.add(str(row.error))
                    if scanned >= limit:
                        break
                    continue
                warnings.update(str(cell) for cell in row if isinstance(cell, CellError))
                rows.append(row)
                if scanned >= limit:
                    break
            return {"header": source.header, "rows": rows, "encoding": options.encoding, "delimiter": options.delimiter,
                    "format": source.format, "sheets": source.sheets, "sheet": source.sheet,
                    "warnings": sorted(warnings), "row_mode": options.row_mode,
                    "preview_rejected": rejected}

    @router.post("/datasets/{dataset_id}/imports", status_code=202, tags=["imports"], dependencies=[Depends(require_admin)])
    def start_import(dataset_id: str, body: ImportCreate):
        require("datasets", dataset_id)
        upload = require("uploads", body.upload_id)
        if upload["state"] != "ready":
            raise HTTPException(409, "Upload is not ready")
        if body.sheet is not None and source_format(upload["filename"]) == "csv":
            raise HTTPException(422, "Worksheet selection is only supported for Excel files")
        if body.row_mode != "standard" and source_format(upload["filename"]) != "csv":
            raise HTTPException(422, "Single-line record mode is only supported for CSV files")
        try:
            return catalog.create_job(dataset_id, body)
        except sqlite3.IntegrityError:
            require("datasets", dataset_id)
            require("uploads", body.upload_id)
            raise HTTPException(409, "Dataset already has an active or paused import")

    @router.get("/jobs", tags=["imports"], dependencies=[Depends(require_admin)])
    def list_jobs(limit: int = QueryParam(100, ge=1, le=200)):
        return {"items": catalog.list("jobs", limit)}

    @router.get("/jobs/{job_id}", tags=["imports"], dependencies=[Depends(require_admin)])
    def get_job(job_id: str):
        return require("jobs", job_id)

    @router.delete("/jobs/{job_id}", tags=["imports"], dependencies=[Depends(require_admin)])
    def delete_job(job_id: str):
        return deletions.delete_job(job_id)

    @router.post("/jobs/{job_id}/{action}", tags=["imports"], dependencies=[Depends(require_admin)])
    def control_job(job_id: str, action: str):
        if action not in ("pause", "resume", "cancel"):
            raise HTTPException(404, "Unknown action")
        with catalog.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "Job not found")
            status = row["status"]
            if action == "resume":
                if status != "paused":
                    raise HTTPException(409, "Only paused tasks can be resumed")
                db.execute("UPDATE jobs SET status='queued',pause_requested=0,cancel_requested=0,error=NULL,updated_at=? WHERE id=?",
                           (now(), job_id))
            elif status in ("completed", "failed", "cancelled"):
                raise HTTPException(409, "Task is already terminal")
            elif action == "cancel":
                new_status = "cancelled" if status in ("queued", "paused") else status
                db.execute("UPDATE jobs SET status=?,cancel_requested=1,updated_at=? WHERE id=?", (new_status, now(), job_id))
            else:
                new_status = "paused" if status == "queued" else status
                db.execute("UPDATE jobs SET status=?,pause_requested=1,updated_at=? WHERE id=?", (new_status, now(), job_id))
            catalog.audit(db, f"import.{action}", job_id)
        return require("jobs", job_id)

    @router.get("/jobs/{job_id}/errors", tags=["imports"], dependencies=[Depends(require_admin)])
    def get_errors(job_id: str):
        job = require("jobs", job_id)
        with deletions.access.hold(job["dataset_id"]):
            require("jobs", job_id)
            return read_errors(job_id)

    def read_errors(job_id):
        path = settings.data_dir / "versions" / f"{job_id}.sqlite"
        if not path.exists():
            return {"items": []}
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='errors'").fetchone():
                return {"items": []}
            items = [{"record": row[0], "reason": row[1]} for row in db.execute("SELECT * FROM errors ORDER BY row_no LIMIT 100")]
        return {"items": items}

    @router.post("/datasets/{dataset_id}/query", tags=["query"])
    def query_dataset(dataset_id: str, body: Query, user: Principal = Depends(authorize)):
        with deletions.access.hold(dataset_id):
            require("datasets", dataset_id)
            result = service.query(dataset_id, body)
            annotations = extractions.query_annotations(user.id, result["version"], [row["_row"] for row in result["rows"]])
            return service.with_annotations(dataset_id, body, result, annotations)

    @router.get("/stats", tags=["service"], dependencies=[Depends(require_admin)])
    def statistics():
        return {"cache": service.cache.stats(), "pending_file_cleanup": deletions.purge_pending(),
                "free_disk_bytes": shutil.disk_usage(settings.data_dir).free,
                "max_upload_bytes": settings.max_upload_bytes, "max_chunk_bytes": settings.max_chunk_bytes,
                "supported_formats": ["csv", "xls", "xlsx"], "max_xls_bytes": settings.max_xls_bytes,
                "max_xlsx_metadata_bytes": settings.max_xlsx_metadata_bytes,
                "query_timeout_seconds": settings.query_timeout, "read_concurrency": settings.read_concurrency}

    app.include_router(router)
    return app


def main():
    import uvicorn
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
