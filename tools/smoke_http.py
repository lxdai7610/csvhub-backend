"""Import the synthetic example into a running API + worker, using actual HTTP."""
import hashlib
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

import httpx
from csvhub.config import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--spawn", action="store_true", help="Run API + worker in this test environment")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("Port must be in 1024..65535")
    root = Path(__file__).resolve().parents[1]
    directory = (args.directory or root / "data").resolve()
    settings = Settings(data_dir=directory)
    token = settings.token() if args.spawn else (directory / "api-token.txt").read_text().strip()
    content = (root / "examples" / "inventory.csv").read_bytes()
    processes = []
    if args.spawn:
        env = os.environ.copy()
        env["CSVHUB_DATA_DIR"], env["CSVHUB_API_TOKEN"] = str(directory), token
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        code = f"import uvicorn; from csvhub.api import create_app; uvicorn.run(create_app(),host='127.0.0.1',port={args.port})"
        processes.append(subprocess.Popen([sys.executable, "-c", code], env=env, creationflags=flags,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        processes.append(subprocess.Popen([sys.executable, "-m", "csvhub.worker"], env=env, creationflags=flags,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        exercise(root, content, token, args.port)
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=10)


def exercise(root, content, token, port):
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {token}"}, trust_env=False) as client:
        ready_deadline = time.monotonic() + 20
        while True:
            try:
                client.get("/health").raise_for_status()
                break
            except httpx.HTTPError:
                if time.monotonic() > ready_deadline:
                    raise
                time.sleep(0.1)
        assert client.get("/health").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/api/v1/datasets", headers={"Authorization": "invalid"}).status_code == 401
        dataset = client.post("/api/v1/datasets", json={"name": "Synthetic inventory demo", "columns": [
            {"name": "record_key", "source": "record_key"},
            {"name": "label", "source": "label"},
            {"name": "event_date", "source": "event_date", "kind": "date"},
            {"name": "quantity", "source": "quantity", "kind": "integer"}]}).json()
        upload = client.post("/api/v1/uploads", json={"filename": "inventory.csv", "size": len(content),
                                                     "sha256": hashlib.sha256(content).hexdigest()}).json()
        response = client.put(f"/api/v1/uploads/{upload['id']}/chunks?offset=0", content=content,
                              headers={"X-Chunk-SHA256": hashlib.sha256(content).hexdigest()})
        response.raise_for_status()
        client.post(f"/api/v1/uploads/{upload['id']}/complete").raise_for_status()
        job = client.post(f"/api/v1/datasets/{dataset['id']}/imports", json={"upload_id": upload["id"]}).json()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = client.get(f"/api/v1/jobs/{job['id']}").json()
            if state["status"] == "completed":
                break
            if state["status"] == "failed":
                raise RuntimeError(state)
            time.sleep(0.1)
        else:
            raise RuntimeError("Importer did not finish within 30 seconds")
        response = client.post(f"/api/v1/datasets/{dataset['id']}/query", json={
            "filters": [{"field": "record_key", "value": "000001"}], "explain": True})
        response.raise_for_status()
        result = response.json()
        assert result["rows"][0]["record_key"] == "000001"
        assert result["rows"][0]["quantity"] == 7
        assert any("SEARCH" in line for line in result["plan"])
        print("Actual HTTP smoke passed; synthetic demo: 3 rows; CSV leading zero and indexed lookup verified.")


if __name__ == "__main__":
    main()
