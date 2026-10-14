from contextlib import closing
from dataclasses import replace
import hashlib
import json
import io
import sqlite3
import threading
import os
import subprocess
import sys
import time

import pytest
from pydantic import ValidationError

from csvhub.cache import ResultCache
from csvhub.config import Settings
from csvhub.locking import file_lock, LockBusy
from csvhub.models import Query
from csvhub.parsing import csv_reader
from csvhub.worker import Worker, JobStopped
from csvhub.models import DatasetCreate, UploadCreate, ImportCreate
from conftest import dataset, upload, import_data


def query(client, dataset_id, **body):
    return client.post(f"/api/v1/datasets/{dataset_id}/query", json=body)


def test_auth_and_health(system):
    client, *_ = system
    assert client.get("/health", headers={"Authorization": "wrong"}).status_code == 200
    assert client.get("/api/v1/datasets", headers={"Authorization": "wrong"}).status_code == 401
    assert client.get("/api/v1/datasets").status_code == 200


def test_frontend_served_with_backend_and_keeps_api_auth(system):
    client, *_ = system
    response = client.get("/", headers={"Authorization": "invalid"})
    assert response.status_code == 200
    assert "CSVHub" in response.text and 'lang="zh-CN"' in response.text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/assets/hash-worker.js").status_code == 200
    assert client.get("/assets/example.csv").text.startswith("record_key")
    assert client.get("/api/v1/datasets", headers={"Authorization": "invalid"}).status_code == 401


def test_csv_quotes_newlines_and_leading_zero(system):
    source = 'key,label\r\n00001,"Comma, inside"\r\n00002,"Two\nlines"\r\n00003,中文\r\n'.encode()
    dataset_id, job = import_data(system, source)
    assert job["status"] == "completed", job
    result = query(system[0], dataset_id).json()
    assert [r["key"] for r in result["rows"]] == ["00001", "00002", "00003"]
    assert result["rows"][1]["label"] == "Two\nlines"
    assert query(system[0], dataset_id).json()["cache_hit"] is True


def test_types_and_error_samples(system):
    client, *_ = system
    dataset_id = dataset(client, [
        {"name": "key", "source": "key"},
        {"name": "date", "source": "date", "kind": "date"},
        {"name": "stock", "source": "stock", "kind": "integer", "nullable": False}])
    dataset_id, job = import_data(system, b"key,date,stock\n00001,20261009,7\n00002,2026-13-01,8\n00003,2026-10-09,0\n",
                                  dataset_id, on_error="skip")
    assert job["status"] == "completed"
    assert (job["processed"], job["accepted"], job["rejected"]) == (3, 2, 1)
    rows = query(client, dataset_id).json()["rows"]
    assert rows[0]["date"] == "2026-10-09"
    assert rows[0]["stock"] == 7
    assert rows[1]["_row"] == 3
    assert client.get(f"/api/v1/jobs/{job['id']}/errors").json()["items"][0]["record"] == 2


def test_exact_prefix_range_and_plan(system):
    client, *_ = system
    dataset_id, job = import_data(system, b"key,label\n003,beta\n001,alpha\n002,alphabet\n004,\n")
    assert job["status"] == "completed"
    prefix = query(client, dataset_id, filters=[{"field": "label", "op": "prefix", "value": "alph"}], explain=True).json()
    assert [r["key"] for r in prefix["rows"]] == ["001", "002"]
    assert any("SEARCH" in p and "ix_1" in p for p in prefix["plan"])
    exact = query(client, dataset_id, filters=[{"field": "key", "value": "003"}]).json()
    assert exact["rows"][0]["label"] == "beta"
    ranged = query(client, dataset_id, filters=[{"field": "key", "op": "gte", "value": "002"},
                                               {"field": "key", "op": "lte", "value": "003"}]).json()
    assert [r["key"] for r in ranged["rows"]] == ["002", "003"]


def test_cursor_pages_no_duplicates_and_tamper_rejected(system):
    client, *_ = system
    dataset_id, _ = import_data(system, b"key,label\n1,b\n2,a\n3,a\n4,b\n5,b\n")
    filters = [{"field": "label", "op": "prefix", "value": "b"}]
    page1 = query(client, dataset_id, filters=filters, limit=2).json()
    page2 = query(client, dataset_id, filters=filters, limit=2, cursor=page1["next_cursor"]).json()
    assert [r["key"] for r in page1["rows"] + page2["rows"]] == ["1", "4", "5"]
    assert page2["next_cursor"] is None
    assert query(client, dataset_id, cursor=page1["next_cursor"]).status_code == 422
    assert query(client, dataset_id, filters=filters, cursor="corrupted").status_code == 422


def test_old_version_visible_until_success_and_cursor_invalidated(system):
    client, catalog, worker, _ = system
    dataset_id, old = import_data(system, b"key,label\n1,old\n2,old\n3,old\n")
    cursor = query(client, dataset_id, limit=1).json()["next_cursor"]
    upload_id = upload(client, b"key,label\n9,new\n")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    job = client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id}).json()
    assert query(client, dataset_id).json()["version"] == old["id"]
    worker.run_one()
    assert catalog.get("jobs", job["id"])["status"] == "completed"
    assert query(client, dataset_id).json()["rows"][0]["key"] == "9"
    assert query(client, dataset_id, limit=1, cursor=cursor).status_code == 422


def test_failed_import_does_not_publish(system):
    client, catalog, *_ = system
    dataset_id, old = import_data(system, b"key,label\n1,old\n")
    _, job = import_data(system, b'key,label\n2,"broken\n', dataset_id)
    assert job["status"] == "failed"
    assert catalog.get("datasets", dataset_id)["active_version"] == old["id"]


def test_resume_uses_durable_checkpoint(system, monkeypatch):
    client, catalog, worker, settings = system
    dataset_id = dataset(client)
    upload_id = upload(client, b"key,label\n1,a\n2,b\n3,c\n4,d\n5,e\n")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    job = client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id}).json()
    normal = worker.sync_progress

    def stop_after_batch(db, job_id):
        normal(db, job_id)
        if catalog.get("jobs", job_id)["processed"] >= 2:
            raise JobStopped("queued", "Simulated process interruption")

    monkeypatch.setattr(worker, "sync_progress", stop_after_batch)
    worker.run_one()
    assert catalog.get("jobs", job["id"])["processed"] == 2
    new_worker = Worker(settings, catalog)
    assert new_worker.run_one()
    finished = catalog.get("jobs", job["id"])
    assert finished["status"] == "completed", finished
    assert finished["accepted"] == 5
    assert len(query(client, dataset_id).json()["rows"]) == 5


def test_pause_resume_cancel_and_active_import_conflict(system):
    client, catalog, worker, _ = system
    dataset_id = dataset(client)
    upload_id = upload(client, b"key,label\n1,a\n")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    body = {"upload_id": upload_id}
    job = client.post(f"/api/v1/datasets/{dataset_id}/imports", json=body).json()
    assert client.post(f"/api/v1/datasets/{dataset_id}/imports", json=body).status_code == 409
    assert client.post(f"/api/v1/jobs/{job['id']}/pause").json()["status"] == "paused"
    assert not worker.run_one()
    assert client.post(f"/api/v1/jobs/{job['id']}/resume").json()["status"] == "queued"
    assert client.post(f"/api/v1/jobs/{job['id']}/cancel").json()["status"] == "cancelled"
    assert not worker.run_one()
    assert client.post(f"/api/v1/jobs/{job['id']}/resume").status_code == 409


def test_chunks_offset_checksum_and_crash_append_recovery(system):
    client, catalog, _, settings = system
    source = b"key,label\n1,a\n"
    body = {"filename": "../../not-a-path.csv", "size": len(source), "sha256": hashlib.sha256(source).hexdigest()}
    upload_id = client.post("/api/v1/uploads", json=body).json()["id"]
    prefix, suffix = source[:5], source[5:]
    url = f"/api/v1/uploads/{upload_id}/chunks"
    assert client.put(url + "?offset=0", content=prefix, headers={"X-Chunk-SHA256": "0" * 64}).status_code == 422
    headers = {"X-Chunk-SHA256": hashlib.sha256(prefix).hexdigest()}
    assert client.put(url + "?offset=0", content=prefix, headers=headers).status_code == 200
    assert client.put(url + "?offset=0", content=prefix, headers=headers).status_code == 409
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 409
    part = settings.data_dir / "uploads" / f"{upload_id}.part"
    with part.open("ab") as file:
        file.write(b"uncommitted crash bytes")
    assert client.put(url + "?offset=5", content=suffix,
                      headers={"X-Chunk-SHA256": hashlib.sha256(suffix).hexdigest()}).status_code == 200
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").json()["state"] == "ready"
    assert (settings.data_dir / "uploads" / f"{upload_id}.csv").read_bytes() == source
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 200


def test_whole_file_checksum_failure(system):
    client, *_ = system
    upload_id = upload(client, b"key,label\n1,a\n", digest="0" * 64)
    assert client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 409
    assert client.get(f"/api/v1/uploads/{upload_id}").json()["state"] == "invalid"


def test_size_limit_and_encoding_preview(system):
    client, catalog, worker, settings = system
    source = "key,label\n00001,中文\n".encode("gb18030")
    upload_id = upload(client, source)
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    assert client.get(f"/api/v1/uploads/{upload_id}/preview").status_code == 422
    result = client.get(f"/api/v1/uploads/{upload_id}/preview?encoding=gb18030").json()
    assert result["rows"][0][1] == "中文"
    assert client.get(f"/api/v1/uploads/{upload_id}/preview?delimiter=xx").status_code == 422
    ds = dataset(client)
    job = client.post(f"/api/v1/datasets/{ds}/imports", json={"upload_id": upload_id, "encoding": "gb18030"}).json()
    worker.run_one()
    assert catalog.get("jobs", job["id"])["status"] == "completed"
    response = client.post("/api/v1/uploads", json={"filename": "huge.csv", "size": settings.max_upload_bytes + 1,
                                                   "sha256": "0" * 64})
    assert response.status_code == 413


def test_invalid_schema_unindexed_and_injection_values(system):
    client, *_ = system
    assert client.post("/api/v1/datasets", json={"name": "test", "columns": [
        {"name": "same", "source": "a"}, {"name": "same", "source": "b"}]}).status_code == 422
    dataset_id = dataset(client, [{"name": "key", "source": "key"}, {"name": "label", "source": "label", "indexed": False}])
    import_data(system, b"key,label\n1,a\n", dataset_id)
    assert query(client, dataset_id, filters=[{"field": "label", "value": "a"}]).json()["rows"][0]["key"] == "1"
    assert query(client, dataset_id, filters=[{"field": "label", "value": "' OR 1=1 --"}]).json()["rows"] == []
    assert query(client, dataset_id, filters=[{"field": "key", "value": "' OR 1=1 --"}]).json()["rows"] == []
    assert query(client, dataset_id, filters=[{"field": "key; DROP TABLE records", "value": "a"}]).status_code == 422
    assert query(client, dataset_id, limit=201).status_code == 422


def test_headers_width_and_oversize_record(system):
    client, catalog, worker, settings = system
    _, job = import_data(system, b"key,key\n1,a\n")
    assert job["status"] == "failed"
    _, job = import_data(system, b"key,label\n1,a,extra\n2,b\n", on_error="skip")
    assert (job["status"], job["accepted"], job["rejected"]) == ("completed", 1, 1)
    with io.BytesIO(b'key,label\n1,"line\nline\nline\nline"\n') as binary:
        text, lines, reader = csv_reader(binary, "utf-8", ",", 12)
        next(reader)
        lines.reset()
        with pytest.raises(ValueError, match="size limit"):
            next(reader)
        text.detach()


def test_cache_budget_and_copy_isolation():
    cache = ResultCache(900, 60)
    cache.put("a", {"rows": [1]})
    result = cache.get("a")
    result["rows"].append(2)
    assert cache.get("a")["rows"] == [1]
    cache.put("b", {"rows": [3]})
    assert cache.stats()["accounted_bytes"] <= 900
    assert cache.get("a") is None
    expired = ResultCache(1000, -1)
    expired.put("x", {"a": 1})
    assert expired.get("x") is None


def test_worker_lock_is_exclusive(tmp_path):
    with file_lock(tmp_path / "worker.lock"):
        with pytest.raises(LockBusy):
            with file_lock(tmp_path / "worker.lock"):
                pass


def test_low_disk_pauses_then_resume(system, monkeypatch):
    client, catalog, worker, settings = system
    dataset_id = dataset(client)
    upload_id = upload(client, b"key,label\n1,a\n")
    client.post(f"/api/v1/uploads/{upload_id}/complete")
    job = client.post(f"/api/v1/datasets/{dataset_id}/imports", json={"upload_id": upload_id}).json()
    monkeypatch.setattr(worker, "check_disk", lambda: (_ for _ in ()).throw(JobStopped("paused", "Low disk space")))
    worker.run_one()
    assert catalog.get("jobs", job["id"])["status"] == "paused"
    client.post(f"/api/v1/jobs/{job['id']}/resume")
    Worker(settings, catalog).run_one()
    assert catalog.get("jobs", job["id"])["status"] == "completed"


def test_query_timeout_capacity(system):
    client, catalog, worker, settings = system
    dataset_id, _ = import_data(system, b"key,label\n1,a\n")
    service = client.app.state.queries
    service.settings = replace(settings, query_timeout=0.01)
    for _ in range(settings.read_concurrency):
        service.slots.acquire()
    try:
        assert query(client, dataset_id, limit=1).status_code == 503
    finally:
        for _ in range(settings.read_concurrency):
            service.slots.release()


def test_large_upload_contract_is_not_32bit(system):
    # Contract validation is separate from the real scale benchmark in tools/benchmark.py.
    from csvhub.models import UploadCreate
    assert UploadCreate(filename="large.csv", size=5 * 1024**3, sha256="0" * 64).size > 2**32


def test_request_body_limits_known_and_streamed(system):
    client, *_ = system
    assert client.post("/api/v1/datasets", content=b"x" * (128 * 1024 + 1)).status_code == 413
    # No Content-Length: body limit must still be checked during streaming.
    blocks = (block for block in (b"x" * 70000, b"x" * 70000))
    response = client.post("/api/v1/datasets", content=blocks, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_numeric_invalid_filter_returns_validation_error(system):
    client, *_ = system
    ds = dataset(client, [{"name": "key", "source": "key", "kind": "integer"}])
    import_data(system, b"key\n1\n", ds)
    assert query(client, ds, filters=[{"field": "key", "value": "not-a-number"}]).status_code == 422


def test_supplementary_unicode_cursor_roundtrip(system):
    client, *_ = system
    label = "😀" * 512
    ds, job = import_data(system, ("key,label\n1," + label + "\n2," + label + "\n").encode())
    assert job["status"] == "completed", job
    filters = [{"field": "label", "op": "prefix", "value": "😀"}]
    first = query(client, ds, filters=filters, limit=1).json()
    second = query(client, ds, filters=filters, limit=1, cursor=first["next_cursor"])
    assert second.status_code == 200, second.text
    assert second.json()["rows"][0]["key"] == "2"


def test_response_size_budget_paginates_early(system):
    client, catalog, worker, settings = system
    ds = dataset(client, [{"name": "key", "source": "key"}, {"name": "label", "source": "label", "indexed": False}])
    content = ("key,label\n" + "".join(f"{i}," + "x" * 100 + "\n" for i in range(5))).encode()
    service = client.app.state.queries
    # The API budget now includes personal annotations and the signed next-page cursor.
    service.settings = replace(settings, max_response_bytes=700)
    import_data(system, content, ds)
    first = query(client, ds, limit=10).json()
    assert 0 < len(first["rows"]) < 5 and first["next_cursor"]
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode()) <= 700
    second = query(client, ds, limit=10, cursor=first["next_cursor"]).json()
    assert second["rows"][0]["_row"] > first["rows"][-1]["_row"]


def test_no_source_values_in_failed_type_error(system):
    client, *_ = system
    ds = dataset(client, [{"name": "quantity", "source": "quantity", "kind": "integer"}])
    _, job = import_data(system, b"quantity\nCONFIDENTIAL_CELL_VALUE\n", ds)
    assert job["status"] == "failed"
    assert "CONFIDENTIAL_CELL_VALUE" not in job["error"]


def test_actual_worker_kill_and_restart(system):
    client, catalog, _, original_settings = system
    settings = replace(original_settings, batch_rows=5000)
    ds = catalog.create_dataset(DatasetCreate(name="Synthetic crash recovery", columns=[
        {"name": "key", "source": "key"}, {"name": "label", "source": "label"}]))
    expected = 200000
    source = b"key,label\n" + b"".join(f"P{i:08d},item\n".encode() for i in range(expected))
    item = catalog.create_upload(UploadCreate(filename="synthetic.csv", size=len(source),
                                             sha256=hashlib.sha256(source).hexdigest()))
    path = settings.data_dir / "uploads" / f"{item['id']}.csv"
    path.write_bytes(source)
    with catalog.transaction() as db:
        db.execute("UPDATE uploads SET state='ready',received=size WHERE id=?", (item["id"],))
    job = catalog.create_job(ds["id"], ImportCreate(upload_id=item["id"]))
    env = os.environ.copy()
    env["CSVHUB_DATA_DIR"] = str(settings.data_dir)
    env["CSVHUB_API_TOKEN"] = settings.api_token
    env["CSVHUB_BATCH_ROWS"] = "1000"
    process = subprocess.Popen([sys.executable, "-m", "csvhub.worker"], env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            progress = catalog.get("jobs", job["id"])
            if progress["status"] == "importing" and progress["processed"] >= 2000:
                break
            assert process.poll() is None
            time.sleep(0.01)
        else:
            pytest.fail("Worker did not reach a committed batch")
        process.kill()  # Forced OS termination, without graceful task cleanup.
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    resumed = Worker(settings, catalog)
    assert resumed.run_one()
    finished = catalog.get("jobs", job["id"])
    assert finished["status"] == "completed", finished
    assert finished["accepted"] == expected and finished["processed"] == expected
    db_path = settings.data_dir / "versions" / f"{job['id']}.sqlite"
    with closing(sqlite3.connect(db_path)) as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == expected
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
