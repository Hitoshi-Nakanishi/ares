import json
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from ares import supervisor, watchdog
from ares.config import MemoryLimits


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(watchdog.time, "monotonic", clock)
    return clock


def test_new_driver_fault_stops_and_an_old_one_does_not(monkeypatch):
    faults = iter([(7, "153", "earlier"), (7, "153", "earlier"), (8, "153", "2026-01-02T03:04:05Z")])
    monkeypatch.setattr(watchdog, "latest_driver_fault", lambda: next(faults))
    guard = watchdog.Watchdog()
    guard.check()
    with pytest.raises(RuntimeError, match="event 153 at 2026-01-02T03:04:05Z"):
        guard.check()


def test_unreadable_driver_log_never_stops_inference(monkeypatch, capsys):
    def unreadable():
        raise OSError("access denied")

    monkeypatch.setattr(watchdog, "latest_driver_fault", unreadable)
    guard = watchdog.Watchdog()
    guard.check()
    assert "driver fault stops are off" in capsys.readouterr().out


def test_engine_stall_stops_only_while_requests_run_without_progress(monkeypatch, clock):
    monkeypatch.setattr(watchdog, "latest_driver_fault", lambda: None)
    samples = iter(
        [
            {"vllm:num_requests_running": 2, "vllm:generation_tokens_total": 10},
            {"vllm:num_requests_running": 2, "vllm:generation_tokens_total": 50},
            {"vllm:num_requests_running": 0, "vllm:generation_tokens_total": 50},
            {"vllm:num_requests_running": 3, "vllm:generation_tokens_total": 50},
            {"vllm:num_requests_running": 3, "vllm:generation_tokens_total": 50},
        ]
    )
    monkeypatch.setattr(watchdog, "engine_metrics", lambda port: next(samples))
    guard = watchdog.Watchdog(54321)
    for _ in range(3):
        guard.check()
        clock.now += 200
    guard.check()
    clock.now += 100
    with pytest.raises(RuntimeError, match="no progress for 300 s with 3 requests running"):
        guard.check()


def test_server_that_stops_answering_after_it_answered_is_stopped(monkeypatch, clock):
    monkeypatch.setattr(watchdog, "latest_driver_fault", lambda: None)
    answers = iter([httpx.ConnectError("loading"), {}, httpx.ReadTimeout("hung"), httpx.ReadTimeout("hung")])

    def metrics(port):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(watchdog, "engine_metrics", metrics)
    guard = watchdog.Watchdog(54321)
    clock.now += 3600
    guard.check()
    guard.check()
    clock.now += 200
    guard.check()
    clock.now += 100
    with pytest.raises(RuntimeError, match="stopped answering for 300 s"):
        guard.check()


def test_engine_metrics_sums_labelled_samples(monkeypatch):
    text = (
        "# HELP vllm:num_requests_running Running requests.\n"
        'vllm:num_requests_running{engine="0",model_name="m"} 2.0\n'
        'vllm:num_requests_running{engine="1",model_name="m"} 1.0\n'
        'vllm:generation_tokens_total{engine="0",model_name="m"} 1.5e3\n'
        'vllm:cache_config_info{block_size="16"} 1.0\n'
    )
    request = httpx.Request("GET", "http://127.0.0.1:54321/metrics")
    monkeypatch.setattr(watchdog.httpx, "get", lambda *args, **kwargs: httpx.Response(200, text=text, request=request))
    assert watchdog.engine_metrics(54321) == {"vllm:num_requests_running": 3.0, "vllm:generation_tokens_total": 1500.0}
    monkeypatch.setattr(watchdog.httpx, "get", lambda *args, **kwargs: httpx.Response(404))
    assert watchdog.engine_metrics(54321) == {}


@pytest.mark.skipif(os.name != "nt", reason="Windows System log")
def test_driver_fault_query_reads_the_windows_system_log():
    fault = watchdog.latest_driver_fault()
    assert fault is None or (fault[0] > 0 and fault[1].isdigit())


def test_watchdog_fault_stops_unmonitored_inference_and_latches_a_pause(tmp_path, monkeypatch):
    class Child:
        pid = 10
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    child = Child()

    class Faulted:
        interval = 0

        def check(self):
            raise RuntimeError("GPU driver fault event 153 at now; inference stopped to free the GPU")

    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kwargs: None)
    monkeypatch.setattr(supervisor, "memory_snapshot", lambda *args: pytest.fail("No memory polling"))
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: child)
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: SimpleNamespace(children=lambda recursive: []))
    record = tmp_path / "llm-memory.json"
    limits = MemoryLimits(enforce_runtime=False)
    assert supervisor.supervise([], tmp_path, record, limits=limits, watchdog=Faulted()) == 1
    assert child.returncode == -9
    state = json.loads(record.read_text())
    assert state["status"] == "stopped"
    assert "event 153" in state["error"]
    assert "event 153" in json.loads((tmp_path / "llm-paused.json").read_text())["reason"]


def test_healthy_watchdog_lets_unmonitored_inference_exit_normally(tmp_path, monkeypatch):
    class Child:
        pid = 10
        returncode = None
        calls = 0

        def poll(self):
            self.calls += 1
            if self.calls > 2:
                self.returncode = 0
            return self.returncode

        def kill(self):
            pytest.fail("A healthy engine must not be stopped")

        def wait(self, timeout=None):
            return self.returncode

    checks = []
    healthy = SimpleNamespace(interval=0, check=lambda: checks.append(True))
    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kwargs: None)
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *args, **kwargs: Child())
    monkeypatch.setattr(supervisor.time, "sleep", lambda seconds: None)
    record = tmp_path / "llm-memory.json"
    limits = MemoryLimits(enforce_runtime=False)
    assert supervisor.supervise([], tmp_path, record, limits=limits, watchdog=healthy) == 0
    assert checks == [True, True]
    assert json.loads(record.read_text())["status"] == "exited"
    assert not (tmp_path / "llm-paused.json").exists()


def test_manager_module_runs_its_command_line():
    result = subprocess.run(
        [sys.executable, "-m", "ares.manager", "--help"], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0
    assert "status" in result.stdout
