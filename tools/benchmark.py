"""End-to-end synthetic scale check; never reads external datasets.

python tools/benchmark.py --directory ./benchmark-work --rows 20000010 --output ./benchmark.json
Requires test extras. Uploads over HTTP TestClient, executes a separate importer process.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import sqlite3
import subprocess
import sys
import threading
import time

import psutil
from fastapi.testclient import TestClient

from csvhub.api import create_app
from csvhub.config import Settings


def percentile(values, percent):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * percent))]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=20000010)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "data" / "catalog.sqlite").exists():
        raise SystemExit("Use a fresh benchmark directory; existing data is never overwritten")
    source = root / "synthetic-inventory.csv"
    started = time.monotonic()
    with source.open("wb") as file:
        file.write(b"record_key,category\n")
        for start in range(1, args.rows + 1, 10000):
            stop = min(args.rows + 1, start + 10000)
            file.write("".join(f"P{i:08d},category{i % 100:02d}\n" for i in range(start, stop)).encode())
    generation_seconds = time.monotonic() - started
    digest = hashlib.sha256()
    with source.open("rb") as file:
        while block := file.read(1024 * 1024):
            digest.update(block)
    settings = Settings(data_dir=root / "data")
    app = create_app(settings)
    token = settings.token()
    headers = {"Authorization": f"Bearer {token}"}
    peak_api_rss, peak_worker_rss = 0, 0
    worker_pid = None
    sample_stop = threading.Event()

    def sample():
        nonlocal peak_api_rss, peak_worker_rss
        while not sample_stop.wait(0.2):
            peak_api_rss = max(peak_api_rss, psutil.Process().memory_info().rss)
            if worker_pid is not None:
                try:
                    peak_worker_rss = max(peak_worker_rss, psutil.Process(worker_pid).memory_info().rss)
                except psutil.Error:
                    pass

    monitor = threading.Thread(target=sample, daemon=True)
    monitor.start()
    process = None
    worker_log = None
    try:
        with TestClient(app, headers=headers) as client:
            upload_start = time.monotonic()
            response = client.post("/api/v1/uploads", json={"filename": source.name, "size": source.stat().st_size,
                                                           "sha256": digest.hexdigest()})
            response.raise_for_status()
            upload_id = response.json()["id"]
            offset = 0
            with source.open("rb") as file:
                while chunk := file.read(settings.max_chunk_bytes):
                    response = client.put(f"/api/v1/uploads/{upload_id}/chunks?offset={offset}", content=chunk,
                                          headers={"X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()})
                    response.raise_for_status()
                    offset += len(chunk)
            client.post(f"/api/v1/uploads/{upload_id}/complete").raise_for_status()
            upload_seconds = time.monotonic() - upload_start
            dataset = client.post("/api/v1/datasets", json={"name": "Synthetic scale benchmark", "columns": [
                {"name": "record_key", "source": "record_key"}, {"name": "category", "source": "category"}]}).json()
            job = client.post(f"/api/v1/datasets/{dataset['id']}/imports", json={"upload_id": upload_id}).json()
            if "id" not in job:
                raise RuntimeError(job)
            worker_log = (root / "worker.log").open("w", encoding="utf-8")
            env = os.environ.copy()
            env["CSVHUB_DATA_DIR"] = str(settings.data_dir)
            env["CSVHUB_API_TOKEN"] = token
            # A single worker process may redirect SQLite sort scratch files on restricted hosts.
            env["CSVHUB_SQLITE_TEMP_DIR"] = str(root / "sqlite-temp")
            process = subprocess.Popen([sys.executable, "-u", "-m", "csvhub.worker"], env=env,
                                       stdout=worker_log, stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            worker_pid = process.pid
            import_start = time.monotonic()
            last_phase, last_report, observed = None, 0, []
            while True:
                state = client.get(f"/api/v1/jobs/{job['id']}").json()
                if state["status"] != last_phase:
                    observed.append({"phase": state["status"], "seconds": round(time.monotonic() - import_start, 3)})
                    last_phase = state["status"]
                if state["status"] in ("completed", "failed", "paused", "cancelled"):
                    break
                if time.monotonic() - last_report >= 20:
                    print(json.dumps({"status": state["status"], "committed": state["processed"],
                                      "worker_rss_mib": round(peak_worker_rss / 1024**2, 1)}), flush=True)
                    last_report = time.monotonic()
                if process.poll() is not None:
                    raise RuntimeError("Importer exited; see worker.log")
                if time.monotonic() - import_start > 3600:
                    raise RuntimeError("Benchmark exceeded one hour")
                time.sleep(0.5)
            import_seconds = time.monotonic() - import_start
            if state["status"] != "completed" or state["accepted"] != args.rows or state["rejected"] != 0:
                raise RuntimeError(state)
            version_path = settings.data_dir / "versions" / f"{job['id']}.sqlite"
            randomizer = random.Random(42)
            keys = [f"P{i:08d}" for i in randomizer.sample(range(1, args.rows + 1), min(100, args.rows))]
            misses, hits = [], []
            query_url = f"/api/v1/datasets/{dataset['id']}/query"
            for samples in (misses, hits):
                for key in keys:
                    start = time.perf_counter()
                    response = client.post(query_url, json={"filters": [{"field": "record_key", "value": key}]})
                    response.raise_for_status()
                    result = response.json()
                    samples.append((time.perf_counter() - start) * 1000)
                    if len(result["rows"]) != 1 or result["rows"][0]["record_key"] != key:
                        raise RuntimeError("Exact query correctness check failed")
            group = client.post(query_url, json={"filters": [{"field": "category", "value": "category07"}],
                                                 "limit": 50, "explain": True}).json()
            if len(group["rows"]) != min(50, len(range(7, args.rows + 1, 100))):
                raise RuntimeError("Category query correctness check failed")
            if not any("SEARCH" in line and "ix_1" in line for line in group["plan"]):
                raise RuntimeError("Category query did not use its index")
            result = {
                "data": "synthetic inventory only", "rows": args.rows, "csv_bytes": source.stat().st_size,
                "database_bytes": version_path.stat().st_size, "generation_seconds": round(generation_seconds, 3),
                "upload_and_checksum_seconds": round(upload_seconds, 3),
                "import_index_verify_warm_seconds": round(import_seconds, 3), "phases": observed,
                "peak_api_process_rss_mib": round(peak_api_rss / 1024**2, 2),
                "peak_importer_process_rss_mib": round(peak_worker_rss / 1024**2, 2),
                "exact_query_result_cache_miss_p50_ms": round(percentile(misses, 0.5), 3),
                "exact_query_result_cache_miss_p95_ms": round(percentile(misses, 0.95), 3),
                "exact_query_result_cache_hit_p50_ms": round(percentile(hits, 0.5), 3),
                "exact_query_result_cache_hit_p95_ms": round(percentile(hits, 0.95), 3),
                "category_query_plan": group["plan"], "cache": client.get("/api/v1/stats").json()["cache"],
                "accepted": state["accepted"], "rejected": state["rejected"],
                "platform": platform.platform(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                "notes": "HTTP TestClient; local files and OS cache may be warm; 2 short text columns, 2 indexes; no concurrency load test",
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    finally:
        sample_stop.set()
        monitor.join(timeout=1)
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        if worker_log:
            worker_log.close()


if __name__ == "__main__":
    main()
