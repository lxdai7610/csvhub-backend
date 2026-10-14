"""Run real browser UI checks against isolated synthetic data; leave user service untouched."""
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen


def main():
    directory = Path(os.environ["FRONTEND_QA_DIR"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CSVHUB_DATA_DIR"] = str(directory / "data")
    env["CSVHUB_API_TOKEN"] = "synthetic-browser-test-token-0000"
    env["FRONTEND_QA_URL"] = "http://127.0.0.1:8012"
    fixture = directory / "inventory-ui.csv"
    import csv
    with fixture.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(["record_key", "label", "event_date", "quantity"])
        for i in range(1, 3501):
            label = '<img src=x onerror="window.__xss=1">' if i == 2 else "Synthetic widget, 中文 " + "x" * 60
            writer.writerow([f"{i:06d}", label, "20261009", i])
    env["FRONTEND_QA_FILE"] = str(fixture)
    env["FRONTEND_QA_CHUNK_BYTES"] = "8388608" if os.environ.get("FRONTEND_QA_SCRIPT") == "excel_qa.mjs" else "131072"
    code = "from csvhub.api import create_app; from csvhub.config import Settings; import os,uvicorn; s=Settings.from_env(); from dataclasses import replace; uvicorn.run(create_app(replace(s,max_chunk_bytes=int(os.environ['FRONTEND_QA_CHUNK_BYTES']),min_free_bytes=0)),host='127.0.0.1',port=8012)"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes = []
    try:
        with (directory / "api.log").open("w") as api_log, (directory / "worker.log").open("w") as worker_log:
            processes.append(subprocess.Popen([sys.executable, "-c", code], env=env, stdout=api_log, stderr=subprocess.STDOUT, creationflags=flags))
            processes.append(subprocess.Popen([sys.executable, "-m", "csvhub.worker"], env=env, stdout=worker_log, stderr=subprocess.STDOUT, creationflags=flags))
            for _ in range(100):
                try:
                    with urlopen(env["FRONTEND_QA_URL"] + "/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("UI test service did not start")
            script = os.environ.get("FRONTEND_QA_SCRIPT", "frontend_qa.mjs")
            result = subprocess.run(["node", str(Path(__file__).with_name(script))], env=env)
            if result.returncode:
                raise SystemExit(result.returncode)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
