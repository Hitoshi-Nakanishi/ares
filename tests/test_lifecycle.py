import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil
import pytest

from ares import lifecycle as strata


def test_server_environment_allows_slow_first_start(monkeypatch):
    monkeypatch.delenv("STRATA_ENGINE_READY_S", raising=False)
    assert strata.server_environment()["STRATA_ENGINE_READY_S"] == "7200"
    monkeypatch.setenv("STRATA_ENGINE_READY_S", "2400")
    assert strata.server_environment()["STRATA_ENGINE_READY_S"] == "2400"
    monkeypatch.setenv("PYTHONPATH", "application-source")
    expected = str(Path(strata.__file__).resolve().parents[1])
    assert strata.server_environment()["PYTHONPATH"].split(os.pathsep) == [expected, "application-source"]


def test_stale_or_unrelated_pid_is_not_owned(tmp_path):
    process = psutil.Process(os.getpid())
    record = tmp_path / "pid.json"
    record.write_text(json.dumps({"pid": process.pid, "created": process.create_time()}))
    assert strata.server_process(record, tmp_path) is None
    assert process.is_running()


def test_running_servers_finds_matching_process_only(tmp_path, monkeypatch):
    class Process:
        def __init__(self, pid, parent_pid, command, cwd):
            self.pid = pid
            self.parent_pid = parent_pid
            self.command = command
            self.directory = cwd

        def cmdline(self):
            return self.command

        def cwd(self):
            return self.directory

        def ppid(self):
            return self.parent_pid

    matching = Process(10, 1, ["python", "-m", "serve.server"], str(tmp_path))
    shim = Process(11, 10, ["python", "-m", "serve.server"], str(tmp_path))
    unrelated = Process(12, 1, ["python", "worker.py"], str(tmp_path))
    monkeypatch.setattr(psutil, "process_iter", lambda: [matching, shim, unrelated])
    assert strata.running_servers(tmp_path) == [matching]


def test_start_claim_is_exclusive_and_recoverable(tmp_path):
    lock_file = tmp_path / "starting.json"
    assert strata.claim_start(lock_file)
    assert not strata.claim_start(lock_file)
    record = json.loads(lock_file.read_text())
    strata.release_start(lock_file, record)
    assert strata.claim_start(lock_file)
    strata.release_start(lock_file, json.loads(lock_file.read_text()))
    assert not lock_file.exists()


@pytest.mark.parametrize("entry", ["serve.server", "script", "ares.supervisor", "ares.supervisor"])
def test_server_identity_uses_absolute_config_when_windows_denies_cwd(tmp_path, entry):
    class Process:
        def cmdline(self):
            target = str(tmp_path / "serve" / "server.py") if entry == "script" else entry
            return ["python", "-m", target, "--config", str(tmp_path / "strata.json")]

        def cwd(self):
            raise psutil.AccessDenied(10)

    process = Process()
    assert strata.matches_server(process, tmp_path)
    assert not strata.matches_server(process, tmp_path / "unrelated")


@pytest.mark.parametrize("running", [True, False])
def test_process_exit_is_verified_when_windows_denies_wait(monkeypatch, running):
    class Process:
        def is_running(self):
            return running

    def denied(*args, **kwargs):
        raise psutil.AccessDenied(10)

    monkeypatch.setattr(psutil, "wait_procs", denied)
    process = Process()
    assert strata.wait_for_exit([process], timeout=0) == ([process] if running else [])


def test_start_refuses_a_different_model_on_the_port(tmp_path, monkeypatch):
    config = tmp_path / "strata.json"
    config.write_text(json.dumps({"model_name": "flash-next", "port": 8000}))
    monkeypatch.setenv("STRATA_HOME", str(tmp_path))
    monkeypatch.delenv("STRATA_CONFIG", raising=False)
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"model": "other", "loaded": True}))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport))
    with pytest.raises(SystemExit, match="Another server owns"):
        strata.main(["start"])
    assert not (tmp_path / "strata-server.pid.json").exists()


def test_start_passes_the_configured_port_to_strata(tmp_path, monkeypatch):
    monkeypatch.setattr(strata, "require_memory_headroom", lambda **kwargs: None)
    (tmp_path / "strata.json").write_text(json.dumps({"model_name": "test", "port": 8123, "host": "0.0.0.0"}))
    monkeypatch.setenv("STRATA_HOME", str(tmp_path))
    monkeypatch.delenv("STRATA_CONFIG", raising=False)
    monkeypatch.setattr(strata, "running_servers", lambda root: [])
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=transport))
    commands = []

    def start(command, **kwargs):
        commands.append(command)
        if os.name == "nt":
            assert kwargs["creationflags"] & subprocess.CREATE_BREAKAWAY_FROM_JOB
        return type("Child", (), {"pid": os.getpid()})()

    monkeypatch.setattr(strata.subprocess, "Popen", start)
    with pytest.raises(SystemExit, match="not ready"):
        strata.main(["start", "--wait", "0"])
    assert commands[0][-2:] == ["--port", "8123"]
    if os.name == "nt":
        assert "ares.supervisor" in commands[0]
    else:
        assert commands[0][commands[0].index("--host") + 1] == "127.0.0.1"


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher job isolation")
def test_detached_service_survives_launchers_job_closing(tmp_path):
    marker = tmp_path / "survived.txt"
    code = """import ctypes, os, subprocess, sys
from ctypes import wintypes
from ares.windows_memory import WindowsMemoryJob, _ExtendedLimits
job = WindowsMemoryJob(commit_gib=0.25)
limits = _ExtendedLimits()
limits.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
assert job.api.SetInformationJobObject(job.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
job.api.GetCurrentProcess.restype = wintypes.HANDLE
assert job.api.AssignProcessToJobObject(job.handle, job.api.GetCurrentProcess())
child_code = 'import pathlib,sys,time; time.sleep(0.5); pathlib.Path(sys.argv[1]).write_text("survived")'
subprocess.Popen([sys.executable, '-c', child_code, sys.argv[1]],
    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_BREAKAWAY_FROM_JOB,
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
os._exit(0)
"""
    subprocess.run(
        [sys.executable, "-c", code, str(marker)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.read_text() == "survived"


@pytest.mark.skipif(os.name != "nt", reason="Windows RAM supervisor")
def test_loaded_legacy_server_is_not_accepted_as_memory_protected(tmp_path, monkeypatch):
    (tmp_path / "strata.json").write_text(json.dumps({"model_name": "test", "port": 8123}))
    monkeypatch.setenv("STRATA_HOME", str(tmp_path))
    monkeypatch.delenv("STRATA_CONFIG", raising=False)
    process = type("Process", (), {"cmdline": lambda self: ["python", "-m", "serve.server"]})()
    monkeypatch.setattr(strata, "server_process", lambda *args: process)
    original = httpx.Client

    def response(request):
        data = {"loaded": True, "model": "test"} if request.url.path == "/health" else {"data": [{"id": "test"}]}
        return httpx.Response(200, json=data)

    monkeypatch.setattr(strata.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(response)))
    with pytest.raises(SystemExit, match="no RAM supervisor"):
        strata.main(["start"])


def test_memory_pressure_refuses_to_launch_strata(tmp_path, monkeypatch):
    (tmp_path / "strata.json").write_text(json.dumps({"model_name": "test", "port": 8123}))
    monkeypatch.setenv("STRATA_HOME", str(tmp_path))
    monkeypatch.delenv("STRATA_CONFIG", raising=False)
    monkeypatch.setattr(strata, "running_servers", lambda root: [])
    original = httpx.Client
    monkeypatch.setattr(
        strata.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(lambda request: httpx.Response(503))),
    )

    def pressure(**kwargs):
        assert kwargs == {"available_gib": 64, "commit_gib": 148}
        raise MemoryError("other applications memory reserve")

    monkeypatch.setattr(strata, "require_memory_headroom", pressure)
    monkeypatch.setattr(strata.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Must not launch"))
    with pytest.raises(SystemExit, match="other applications memory reserve"):
        strata.main(["start", "--wait", "0"])
