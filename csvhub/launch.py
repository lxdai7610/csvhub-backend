"""Manage both project services from one controller, independent of access address."""
import argparse
import json
import os
from ipaddress import ip_address
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError, HTTPError
from urllib.request import build_opener, ProxyHandler
import webbrowser

import psutil

from .config import Settings, project_environment
from .locking import file_lock, LockBusy
from . import __version__

URL = "http://127.0.0.1:8000"
MODULES = ("api", "worker")


def service_url(settings):
    address = ip_address(settings.host)
    host = ("127.0.0.1" if address.version == 4 else "::1") if address.is_unspecified else str(address)
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{settings.port}"


def get_json(path):
    try:
        with build_opener(ProxyHandler({})).open(URL + path, timeout=1) as response:
            return json.load(response)
    except (URLError, HTTPError, OSError, ValueError):
        return None


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def read_record(settings):
    path = settings.data_dir / "logs" / "last-start.json"
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RuntimeError("Service record cannot be read; no processes were changed") from error
    if not isinstance(state, dict):
        raise RuntimeError("Invalid service record; no processes were changed")
    return state


def write_record(settings, state):
    directory = settings.data_dir / "logs"
    directory.mkdir(exist_ok=True)
    temporary = directory / "last-start.json.tmp"
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(directory / "last-start.json")


def module_name(process):
    command = process.cmdline()
    for index in range(len(command) - 1):
        if command[index] == "-m" and command[index + 1] in ("csvhub.api", "csvhub.worker"):
            return command[index + 1].rsplit(".", 1)[1]
    return None


def matches(process, settings, project, module, recorded=False):
    if module_name(process) != module or canonical(process.cwd()) != canonical(project):
        return False
    # Interpreter upgrades remain manageable; ownership is the project and data directory.
    environment_reader = getattr(process, "environ", None)
    if environment_reader is None:
        # Legacy process providers may only expose the recorded interpreter identity.
        return recorded and canonical(process.exe()) == canonical(sys.executable)
    environment = environment_reader()
    directory_value = environment.get("CSVHUB_DATA_DIR")
    if directory_value is None:
        directory_value = project_environment(project).get("CSVHUB_DATA_DIR", "data")
    directory = Path(directory_value)
    if not directory.is_absolute():
        directory = project / directory
    return canonical(directory) == canonical(settings.data_dir)


def owned_services(settings, project):
    state = read_record(settings)
    services = {module: {} for module in MODULES}
    # Validate the complete record before allowing any stop operation.
    for module in MODULES:
        pid = state.get(f"{module}_pid")
        if pid is None:
            continue
        if type(pid) is not int or pid <= 0:
            raise RuntimeError("Invalid service PID; no processes were changed")
        try:
            process = psutil.Process(pid)
            started = state.get(f"{module}_started_at")
            if started is not None and abs(process.create_time() - started) > 0.01:
                continue
            if not matches(process, settings, project, module, recorded=True):
                raise RuntimeError(f"Recorded {module} PID is not owned by this project; no processes were changed")
            services[module][pid] = process
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied as error:
            raise RuntimeError("Cannot inspect a project service; run the script with its original privileges") from error
    # Recover services started directly or left running before a record was saved.
    for process in psutil.process_iter():
        try:
            module = module_name(process)
            if module and matches(process, settings, project, module):
                services[module][process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {module: list(processes.values()) for module, processes in services.items()}, state


def terminate(process):
    try:
        process.terminate()
        try:
            process.wait(timeout=10)
        except psutil.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    except psutil.NoSuchProcess:
        pass


def stop_owned_services(settings, project):
    services, state = owned_services(settings, project)
    try:
        with file_lock(settings.data_dir / "worker.lock"):
            worker_running = False
    except LockBusy:
        worker_running = True
    if worker_running and not services["worker"]:
        raise RuntimeError("An importer is still running but its identity cannot be verified; run stop.cmd with its original privileges")
    if not any(services.values()):
        if get_json("/health"):
            prefix = "No startup record or verified service" if not state else "No verified project service is visible"
            raise RuntimeError(f"{prefix}; run stop.cmd on the deployment machine with the original privileges")
        write_record(settings, {**state, "api_pid": None, "worker_pid": None, "status": "stopped"})
        return
    marker = settings.data_dir / "service-stop.request"
    if marker.is_symlink():
        raise RuntimeError("Service stop marker is not safe to write; no processes were changed")
    marker.write_text("stop\n", encoding="ascii")
    for process in services["api"]:
        terminate(process)
    for process in services["worker"]:
        try:
            process.wait(timeout=3)
        except psutil.TimeoutExpired:
            terminate(process)
        except psutil.NoSuchProcess:
            pass
    write_record(settings, {**state, "api_pid": None, "worker_pid": None, "status": "stopped"})


def restart_owned_services(settings, project):
    stop_owned_services(settings, project)


def start_services(settings, project):
    services, _ = owned_services(settings, project)
    if any(len(items) > 1 for items in services.values()):
        raise RuntimeError("Multiple project services are running; use restart.cmd to consolidate them")
    marker = settings.data_dir / "service-stop.request"
    if marker.exists() and services["worker"]:
        raise RuntimeError("A previous stop has not finished; use restart.cmd")
    marker.unlink(missing_ok=True)
    settings.token()
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    children = []
    processes = {module: items[0] if items else None for module, items in services.items()}
    if processes["api"]:
        environment = processes["api"].environ()
        if (environment.get("CSVHUB_HOST", "0.0.0.0") != settings.host
                or int(environment.get("CSVHUB_PORT", "8000")) != settings.port):
            raise RuntimeError("The running service uses a different listening configuration; use restart.cmd")

    def save():
        state = {"project": str(project), "data_dir": str(settings.data_dir.resolve()),
                 "url": URL + "/", "host": settings.host, "port": settings.port, "status": "running"}
        for module, process in processes.items():
            state[f"{module}_pid"] = process.pid if process else None
            state[f"{module}_started_at"] = process.create_time() if process else None
        write_record(settings, state)

    def spawn(module):
        environment = os.environ.copy()
        environment["CSVHUB_DATA_DIR"] = str(settings.data_dir.resolve())
        environment["CSVHUB_HOST"], environment["CSVHUB_PORT"] = settings.host, str(settings.port)
        with (log_dir / f"{module}.log").open("ab") as log:
            child = subprocess.Popen(
                [sys.executable, "-u", "-m", f"csvhub.{module}"], cwd=project,
                env=environment, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
        children.append(child)
        processes[module] = psutil.Process(child.pid)
        save()

    try:
        schema = get_json("/openapi.json")
        if schema is not None:
            if schema.get("info", {}).get("title") != "CSVHub Backend" or not processes["api"]:
                raise RuntimeError(f"Port {settings.port} is occupied by an unverified service")
            if schema.get("info", {}).get("version") != __version__:
                raise RuntimeError("An older project service is running; use restart.cmd")
        elif processes["api"]:
            raise RuntimeError("The project API is running at another address or is unavailable; use restart.cmd")
        else:
            spawn("api")
        try:
            with file_lock(settings.data_dir / "worker.lock"):
                worker_running = False
        except LockBusy:
            worker_running = True
        if worker_running and not processes["worker"]:
            raise RuntimeError("An unverified importer is using this data directory")
        if not processes["worker"]:
            spawn("worker")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children) or any(not p.is_running() for p in processes.values()):
                raise RuntimeError("A service exited during startup; inspect data/logs")
            health = get_json("/health")
            schema = get_json("/openapi.json") if health else None
            try:
                with file_lock(settings.data_dir / "worker.lock"):
                    importer_ready = False
            except LockBusy:
                importer_ready = True
            if (importer_ready and health and health.get("status") == "ok" and health.get("version") == __version__
                    and schema and schema.get("info", {}).get("title") == "CSVHub Backend"
                    and schema.get("info", {}).get("version") == __version__):
                save()
                print("CSVHub API and importer are running: " + URL + "/")
                print(f"Listening on: {settings.host}:{settings.port}")
                if ip_address(settings.host).is_unspecified:
                    print(f"Other computers: http://SERVER_IP:{settings.port}/")
                webbrowser.open(URL + "/")
                return
            time.sleep(0.2)
        raise RuntimeError("Services did not become ready within 20 seconds; inspect data/logs")
    except Exception:
        for child in children:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        raise


def main():
    global URL
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("start", "restart", "stop"), default="start")
    legacy = parser.add_mutually_exclusive_group()
    legacy.add_argument("--restart", action="store_true", help=argparse.SUPPRESS)
    legacy.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    action = "restart" if args.restart else "stop" if args.stop else args.action
    project = Path(__file__).resolve().parents[1]
    try:
        settings = Settings.from_env()
        URL = service_url(settings)
        settings.prepare()
        (settings.data_dir / "logs").mkdir(exist_ok=True)
        with file_lock(settings.data_dir / "service-control.lock"):
            if action in ("stop", "restart"):
                stop_owned_services(settings, project)
            if action == "stop":
                print("CSVHub API and importer stopped. Saved data and import checkpoints are retained.")
            else:
                start_services(settings, project)
    except Exception as error:
        print(f"Service {action} failed: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
