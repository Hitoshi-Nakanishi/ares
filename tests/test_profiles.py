import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from ares import benchmark, manager, runner, supervisor
from ares.backends import build_spec
from ares.config import MemoryLimits, load_profile


def profile_file(tmp_path, **changes):
    native = tmp_path / "native.json"
    native.write_text(json.dumps({"model_name": "local-model", "args": ["--resident-budget-gib", "32"]}))
    data = {
        "version": 1,
        "state_dir": "state",
        "backends": {"strata": {"home": str(tmp_path), "config": str(native), "options": {"resident_budget_gib": 8}}},
    }
    data.update(changes)
    path = tmp_path / "server.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_default_backend_and_inactive_environment_are_safe(tmp_path):
    path = profile_file(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["backends"]["ollama"] = {"model": "${UNCONFIGURED_INACTIVE_MODEL}"}
    path.write_text(yaml.safe_dump(data))
    profile = load_profile(path)
    assert profile["backend"] == "strata"
    assert profile["memory"]["ram_gib"] == 32
    with pytest.raises(ValueError, match="UNCONFIGURED_INACTIVE_MODEL"):
        load_profile(path, "ollama")
    assert load_profile(path, "ollama", resolve_settings=False)["settings"] == {}


@pytest.mark.parametrize(
    "changes",
    [
        {"host": "0.0.0.0"},
        {"port": 0},
        {"port": True},
        {"backend": "automatic"},
        {"memory": {"ram_gb": 48}},
        {"memory": {"ram_gib": -1}},
        {"memory": {"job_commit_gib": 1}},
        {"memory": {"startup_available_gib": 1}},
        {"memory": {"ram_gib": float("nan")}},
    ],
)
def test_invalid_profile_is_rejected_before_launch(tmp_path, changes):
    with pytest.raises(ValueError):
        load_profile(profile_file(tmp_path, **changes))


def test_strata_overrides_are_private_and_preserve_original(tmp_path):
    profile = load_profile(profile_file(tmp_path))
    original = Path(profile["settings"]["config"]).read_bytes()
    spec = build_spec(profile, materialize=True)
    native = json.loads((Path(profile["state_dir"]) / "strata-effective.json").read_text())
    assert native["args"] == ["--resident-budget-gib", "8"]
    assert native["host"] == "127.0.0.1"
    assert spec["command"][spec["command"].index("--host") + 1] == "127.0.0.1"
    assert Path(profile["settings"]["config"]).read_bytes() == original


def test_vllm_container_is_local_bounded_and_offline(tmp_path):
    profile = {
        "backend": "vllm",
        "port": 54321,
        "state_dir": str(tmp_path),
        "memory": asdict(MemoryLimits()),
        "settings": {
            "mode": "docker",
            "image": "local-image:installed",
            "model_path": str(tmp_path),
            "model": "local-model",
        },
    }
    spec = build_spec(profile)
    command = spec["command"]
    assert "127.0.0.1:54321:8000" in command
    assert "--memory=32g" in command and "--memory-swap=32g" in command
    assert "--pull=never" in command and "HF_HUB_OFFLINE=1" in command
    assert command[command.index("--gpu-memory-utilization") + 1] == "0.9"
    assert command[command.index("--max-num-seqs") + 1] == "1"
    assert "--swap-space" not in command


def test_ollama_is_local_single_model_and_local_only(tmp_path):
    spec = build_spec(
        {
            "backend": "ollama",
            "port": 54321,
            "state_dir": str(tmp_path),
            "memory": asdict(MemoryLimits()),
            "settings": {"model": "local-model"},
        }
    )
    assert spec["command"] == ["ollama", "serve"]
    assert spec["env"]["OLLAMA_HOST"] == "127.0.0.1:54321"
    assert spec["env"]["OLLAMA_NO_CLOUD"] == "1"
    assert spec["env"]["OLLAMA_NUM_PARALLEL"] == "1"


@pytest.mark.parametrize("backend", ["strata", "vllm", "ollama"])
def test_readiness_checks_backend_health_and_exact_model(monkeypatch, backend):
    real_client = httpx.Client

    def respond(request):
        if request.url.path == "/health":
            return httpx.Response(200, json={"loaded": True})
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "local-model"}]})
        return httpx.Response(200, json={"version": "test"})

    monkeypatch.setattr(manager.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond)))
    assert manager.readiness({"backend": backend, "port": 54321, "model": "local-model"})
    assert not manager.readiness({"backend": backend, "port": 54321, "model": "other-model"})


def test_container_cleanup_never_stops_an_unowned_container(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="different-owner")

    monkeypatch.setattr(runner.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="unrelated"):
        runner.stop_container({"docker_name": "ares-vllm", "docker_owner": "expected-owner"})
    assert len(calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows job cap and physical memory override")
def test_memory_override_is_used_by_live_supervision(tmp_path, monkeypatch):
    class Child:
        pid = 10
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = 1

        def wait(self, timeout):
            return self.returncode

    child = Child()
    checks, caps = [], []
    monkeypatch.setattr(supervisor, "require_memory_headroom", lambda **kw: checks.append(kw))
    monkeypatch.setattr(
        supervisor,
        "WindowsMemoryJob",
        lambda **kw: caps.append(kw) or SimpleNamespace(resume=lambda p: None, close=lambda: None),
    )
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *a, **kw: child)
    monkeypatch.setattr(supervisor.psutil, "Process", lambda pid: child)
    monkeypatch.setattr(supervisor, "memory_snapshot", lambda p: {"rss": 49 * 2**30, "commit": 60 * 2**30})
    limits = MemoryLimits(ram_gib=48, startup_available_gib=64, job_commit_gib=110)
    assert supervisor.supervise(["local-engine"], tmp_path, tmp_path / "memory.json", limits=limits) == 1
    assert checks[0] == {"available_gib": 64, "commit_gib": 158}
    assert caps == [{"commit_gib": 110}]
    assert json.loads((tmp_path / "memory.json").read_text())["ram_limit_gib"] == 48


def test_benchmark_records_usage_and_checks_answers_without_saving_response(monkeypatch):
    real_client = httpx.Client
    answers = iter(("2", "OK", "123"))

    def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": next(answers)}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 2},
            },
        )

    monkeypatch.setattr(benchmark.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond)))
    rows = benchmark.measure({"backend": "strata", "model": "local-model", "port": 54321}, repetitions=1)
    assert all(row["correct"] and row["completion_tokens"] == 2 for row in rows)
    assert all("content" not in row and "prompt" not in row for row in rows)
