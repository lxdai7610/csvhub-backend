import json
from pathlib import Path
import sys

import psutil
import pytest

from csvhub.config import Settings
from csvhub import launch


def write_record(tmp_path, state):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "last-start.json").write_text(json.dumps(state), encoding="utf-8")


class RecordedProcess:
    def __init__(self, project, module):
        self.project, self.module, self.stopped = project, module, False

    def cwd(self):
        return str(self.project)

    def exe(self):
        return sys.executable

    def cmdline(self):
        return [sys.executable, "-u", "-m", f"csvhub.{self.module}"]

    def terminate(self):
        self.stopped = True

    def wait(self, timeout):
        assert self.stopped


def test_restart_validates_all_owners_before_stopping_any(tmp_path, monkeypatch):
    project = tmp_path / "project"
    api = RecordedProcess(project, "api")
    foreign_worker = RecordedProcess(tmp_path / "different-project", "worker")
    write_record(tmp_path, {"api_pid": 101, "worker_pid": 102})
    monkeypatch.setattr(psutil, "Process", {101: api, 102: foreign_worker}.__getitem__)
    with pytest.raises(RuntimeError, match="not owned"):
        launch.restart_owned_services(Settings(data_dir=tmp_path), project)
    assert not api.stopped
    assert not foreign_worker.stopped


def test_restart_stops_verified_owner_and_skips_expired_pid(tmp_path, monkeypatch):
    project = tmp_path / "project"
    api = RecordedProcess(project, "api")
    write_record(tmp_path, {"api_pid": 101, "worker_pid": 102})

    def process(pid):
        if pid == 102:
            raise psutil.NoSuchProcess(pid)
        return api

    monkeypatch.setattr(psutil, "Process", process)
    launch.restart_owned_services(Settings(data_dir=tmp_path), project)
    assert api.stopped


def test_restart_rejects_reused_pid_with_different_module(tmp_path, monkeypatch):
    project = tmp_path / "project"
    unrelated = RecordedProcess(project, "unrelated")
    write_record(tmp_path, {"api_pid": 101})
    monkeypatch.setattr(psutil, "Process", lambda pid: unrelated)
    with pytest.raises(RuntimeError, match="not owned"):
        launch.restart_owned_services(Settings(data_dir=tmp_path), project)
    assert not unrelated.stopped


def test_restart_does_not_stop_unrecorded_running_service(tmp_path, monkeypatch):
    monkeypatch.setattr(launch, "get_json", lambda path: {"status": "ok"})
    with pytest.raises(RuntimeError, match="No startup record"):
        launch.restart_owned_services(Settings(data_dir=tmp_path), Path(tmp_path))
