import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes
from types import SimpleNamespace

import psutil
import pytest

from ares import supervisor
from ares.config import MemoryLimits
from ares.windows_memory import WindowsMemoryJob


@pytest.mark.parametrize("exit_code", [0, 3])
def test_disabled_runtime_limits_record_failed_processes_without_memory_monitoring(tmp_path, monkeypatch, exit_code):
    class Child:
        pid = 10

        def wait(self, timeout=None):
            return exit_code

        def poll(self):
            return exit_code

        def kill(self):
            pytest.fail("Runtime monitoring is disabled")

    checks = []

    def start(command, **kwargs):
        assert kwargs["creationflags"] == 0
        return Child()

    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kw: checks.append(kw))
    monkeypatch.setattr(supervisor, "WindowsMemoryJob", lambda **kw: pytest.fail("No runtime job memory limit"))
    monkeypatch.setattr(supervisor, "memory_snapshot", lambda *a: pytest.fail("No memory polling"))
    monkeypatch.setattr(supervisor.subprocess, "Popen", start)
    record = tmp_path / "memory.json"
    assert supervisor.supervise([], tmp_path, record, limits=MemoryLimits(enforce_runtime=False)) == exit_code
    assert checks == [{"available_gib": 64, "commit_gib": 148}]
    state = json.loads(record.read_text())
    assert state["enforce_runtime"] is False
    assert state["job_commit_limit_gib"] is None
    assert state["exit_code"] == exit_code
    pause = tmp_path / "llm-paused.json"
    assert pause.exists() is (exit_code != 0)
    if exit_code:
        assert json.loads(pause.read_text())["reason"] == f"Inference exited with code {exit_code}"


def test_memory_snapshot_separates_physical_ram_and_commit():
    class Process:
        def __init__(self, pid, rss, commit):
            self.pid, self.rss, self.commit = pid, rss, commit

        def memory_info(self):
            return SimpleNamespace(rss=self.rss, vms=self.commit)

        def name(self):
            return "strata.exe"

        def children(self, recursive):
            assert recursive
            return [Process(2, 20, 90)]

    snapshot = supervisor.memory_snapshot(Process(1, 2, 3))
    assert snapshot["rss"] == 22
    assert snapshot["commit"] == 93
    assert len(snapshot["processes"]) == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows reader denies file replacement")
def test_snapshot_reader_lock_does_not_interrupt_supervision(tmp_path):
    record = tmp_path / "memory.json"
    record.write_text('{"status":"old"}')
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateFileW(str(record), 0x80000000, 0x00000003, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    try:
        assert supervisor.save_snapshot(record, {"status": "new"}) is False
        assert json.loads(record.read_text())["status"] == "old"
    finally:
        api.CloseHandle(handle)
    assert supervisor.save_snapshot(record, {"status": "new"}) is True
    assert json.loads(record.read_text())["status"] == "new"


@pytest.mark.skipif(os.name != "nt", reason="Windows supervisor entry point")
def test_unavailable_snapshot_does_not_kill_healthy_inference(tmp_path, monkeypatch, capsys):
    class Child:
        pid = 10
        returncode = 0
        calls = 0

        def poll(self):
            self.calls += 1
            return None if self.calls == 1 else 0

        def kill(self):
            pytest.fail("Telemetry failure must not kill inference")

        def wait(self, timeout):
            return 0

    child = Child()
    checks = []
    (tmp_path / "strata.json").write_text(json.dumps({"host": "0.0.0.0"}))

    def start(command, **kwargs):
        assert command[command.index("--host") + 1] == "127.0.0.1"
        return child

    monkeypatch.setattr(
        supervisor,
        "WindowsMemoryJob",
        lambda **kwargs: SimpleNamespace(resume=lambda process: None, close=lambda: None),
    )
    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kwargs: checks.append(kwargs))
    monkeypatch.setattr(supervisor, "Watchdog", lambda: SimpleNamespace(interval=15, check=lambda: None))
    monkeypatch.setattr(supervisor.subprocess, "Popen", start)
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: child)
    monkeypatch.setattr(supervisor, "memory_snapshot", lambda process: {"rss": 2**30, "commit": 90 * 2**30})
    monkeypatch.setattr(supervisor, "save_snapshot", lambda *args: False)
    assert supervisor.main(["--config", str(tmp_path / "strata.json"), "--port", "54321"]) == 0
    assert len(checks) == 2
    assert checks[1] == {"available_gib": 32, "commit_gib": 48}
    assert "live memory limits remain active" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows job memory enforcement")
def test_job_rejects_excess_allocation_and_cleans_up_child():
    job = WindowsMemoryJob(commit_gib=0.125)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import psutil\nprint(int(psutil.Process().nice()))\ntry:\n x=bytearray(256*1024**2)\nexcept MemoryError:\n print('bounded')\nelse:\n raise SystemExit('memory limit missing')",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=0x00000004,
    )
    try:
        job.resume(child)
        output, error = child.communicate(timeout=20)
        assert child.returncode == 0, error
        assert output.splitlines() == [b"16384", b"bounded"]
    finally:
        job.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
    assert not psutil.pid_exists(child.pid)


@pytest.mark.skipif(os.name != "nt", reason="Windows supervisor entry point")
def test_supervisor_stops_tree_at_physical_ram_cutoff(tmp_path, monkeypatch):
    class Child:
        pid = 10
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = 1

        def wait(self, timeout):
            assert timeout == 15
            return self.returncode

    child = Child()
    closed = []
    job = SimpleNamespace(resume=lambda process: None, close=lambda: closed.append(True))
    monkeypatch.setattr(supervisor, "WindowsMemoryJob", lambda **kwargs: job)
    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kwargs: None)
    monkeypatch.setattr(supervisor, "Watchdog", lambda: SimpleNamespace(interval=15, check=lambda: None))
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: child)
    monkeypatch.setattr(supervisor, "memory_snapshot", lambda process: {"rss": 33 * 2**30, "commit": 90 * 2**30})
    assert supervisor.main(["--config", str(tmp_path / "strata.json"), "--port", "54321"]) == 1
    state = json.loads((tmp_path / "strata-memory.json").read_text())
    assert state["status"] == "stopped"
    assert "physical RAM exceeded 32 GiB" in state["error"]
    assert closed == [True]
    assert child.returncode == 1
    pause = json.loads((tmp_path / "llm-paused.json").read_text())
    assert "physical RAM exceeded" in pause["reason"]


def test_startup_memory_failure_is_recorded_and_inhibits_retry(tmp_path, monkeypatch):
    def insufficient(**kwargs):
        raise MemoryError("protected host reserve")

    monkeypatch.setattr(supervisor, "require_memory_headroom", insufficient)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **kw: pytest.fail("Must not launch"))
    record = tmp_path / "llm-memory.json"
    assert supervisor.supervise([], tmp_path, record) == 1
    assert json.loads(record.read_text())["status"] == "stopped"
    assert json.loads((tmp_path / "llm-paused.json").read_text())["reason"] == "protected host reserve"
