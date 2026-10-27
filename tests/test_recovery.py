import json
import socket
from types import SimpleNamespace

import pytest

from ares import manager, recovery
from ares.config import RecoveryPolicy, load_profile


@pytest.mark.parametrize("reason", ["Operator stopped inference", "physical RAM exceeded", "GPU memory low", "unknown"])
def test_recovery_keeps_operator_and_other_safety_stops(tmp_path, reason):
    assert recovery.recovery_blocker(tmp_path, {"reason": reason}, RecoveryPolicy(enabled=True), 1000)


def test_recovery_requires_cooldown_and_enforces_persistent_attempt_budget(tmp_path, monkeypatch):
    policy = RecoveryPolicy(enabled=True)
    pause = {"reason": "GPU driver fault event 153", "created_at": 0}
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="GPU-test\n"))
    assert recovery.recovery_blocker(tmp_path, pause, policy, 100) == "recovery cooldown"
    for now in (1000, 1400, 1800):
        assert recovery.recovery_blocker(tmp_path, pause, policy, now) is None
        recovery.record_attempt(tmp_path, policy, now)
        assert recovery.recovery_blocker(tmp_path, pause, policy, now + 1)
    assert recovery.recovery_blocker(tmp_path, pause, policy, 2200) == "recovery attempt limit reached"
    assert recovery.recovery_blocker(tmp_path, pause, policy, 6000) is None


def test_recovery_refuses_unresponsive_gpu_and_corrupt_history(tmp_path, monkeypatch):
    pause = {"reason": "Engine made no progress for 300 s", "created_at": 0}
    policy = RecoveryPolicy(enabled=True)
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert recovery.recovery_blocker(tmp_path, pause, policy, 1000) == "GPU driver is not responding"
    (tmp_path / "llm-recovery.json").write_text('{"attempts": ["invalid"]}')
    with pytest.raises(ValueError):
        recovery.recovery_blocker(tmp_path, pause, policy, 1000)


def test_disabled_recovery_does_not_launch_or_modify_pause(tmp_path):
    profile = tmp_path / "server.yaml"
    profile.write_text("backend: vllm\nstate_dir: state\nbackends:\n  vllm: {}\n")
    assert manager.main(["recover", "--config", str(profile)]) == 0
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("backend", ["strata", "ollama"])
def test_recovery_cannot_enable_a_different_backend(tmp_path, backend):
    profile = tmp_path / "server.yaml"
    profile.write_text(f"backend: {backend}\nrecovery:\n  enabled: true\n")
    with pytest.raises(ValueError, match="only for"):
        load_profile(profile)


def test_recovery_with_unwritable_history_refuses_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(recovery, "save_snapshot", lambda *args: False)
    with pytest.raises(OSError, match="persist"):
        recovery.record_attempt(tmp_path, RecoveryPolicy(enabled=True), 1000)


def test_manager_recovery_never_clears_an_operator_pause(tmp_path, monkeypatch):
    profile = tmp_path / "server.yaml"
    profile.write_text("backend: vllm\nstate_dir: state\nrecovery:\n  enabled: true\nbackends:\n  vllm: {}\n")
    state = tmp_path / "state"
    state.mkdir()
    original = json.dumps({"reason": "Operator stopped inference", "created_at": 0})
    (state / "llm-paused.json").write_text(original)
    monkeypatch.setattr(manager, "build_spec", lambda *a, **k: {})
    assert manager.main(["recover", "--config", str(profile)]) == 0
    assert (state / "llm-paused.json").read_text() == original


@pytest.mark.parametrize("memory_available", [False, True])
def test_manager_recovers_same_profile_only_after_memory_admission(tmp_path, monkeypatch, memory_available):
    profile = tmp_path / "server.yaml"
    profile.write_text("backend: vllm\nstate_dir: state\nrecovery:\n  enabled: true\nbackends:\n  vllm: {}\n")
    state = tmp_path / "state"
    state.mkdir()
    pause = state / "llm-paused.json"
    pause.write_text(json.dumps({"reason": "GPU driver fault event 153", "created_at": 0}))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    spec = {"backend": "vllm", "port": port, "model": "example-model"}
    monkeypatch.setattr(manager, "build_spec", lambda *a, **k: spec)
    monkeypatch.setattr(manager, "claim_start", lambda _: True)
    monkeypatch.setattr(recovery.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="GPU-test"))
    answers = iter([False, True])
    monkeypatch.setattr(manager, "readiness", lambda _: next(answers))

    def memory(**kwargs):
        if not memory_available:
            raise MemoryError("Insufficient host reserves")

    monkeypatch.setattr(manager, "require_memory_headroom", memory)
    launched = []

    def launch(command, *args):
        launched.append(command)
        return SimpleNamespace(pid=123)

    monkeypatch.setattr(manager, "launch_background", launch)
    monkeypatch.setattr(
        manager.psutil, "Process", lambda _: SimpleNamespace(create_time=lambda: 100, is_running=lambda: True)
    )
    if not memory_available:
        with pytest.raises(SystemExit, match="reserves"):
            manager.main(["recover", "--config", str(profile)])
        assert pause.exists() and not launched
        assert not (state / "llm-recovery.json").exists()
    else:
        assert manager.main(["recover", "--config", str(profile)]) == 0
        assert len(launched) == 1 and not pause.exists()
        assert len(json.loads((state / "llm-recovery.json").read_text())["attempts"]) == 1
        assert json.loads((state / "llm-server.pid.json").read_text())["spec"]["model"] == "example-model"
