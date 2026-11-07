import pytest
import yaml

from ares import coder, lifecycle


@pytest.fixture
def profile(tmp_path):
    path = tmp_path / "server.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "backend": "vllm",
                "port": 54321,
                "state_dir": "state",
                "backends": {
                    "vllm": {
                        "mode": "docker",
                        "image": "engine:1",
                        "volume": "models",
                        "model_path": "m",
                        "model": "local-model",
                    }
                },
            }
        )
    )
    (tmp_path / "state").mkdir()
    return path


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(coder, "readiness", lambda spec: True)
    monkeypatch.setattr(coder.shutil, "which", lambda name: name)
    monkeypatch.setattr(coder.subprocess, "call", lambda command, env=None: seen.append((command, env)) or 0)
    return seen


def test_claude_reaches_the_served_model_and_keeps_agent_arguments(profile, calls):
    assert lifecycle.main(["coder", "--config", str(profile), "claude", "-p", "on", "--allowedTools", "Read"]) == 0
    command, env = calls[0]
    assert command == ["claude", "--effort", "medium", "-p", "on", "--allowedTools", "Read"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:54321"
    assert env["ANTHROPIC_MODEL"] == env["ANTHROPIC_SMALL_FAST_MODEL"] == "local-model"
    assert env["ANTHROPIC_API_KEY"] == ""


def test_codex_overrides_provider_without_touching_its_config(profile, calls, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert coder.main(["--config", str(profile), "--effort", "low", "codex", "exec", "hi"]) == 0
    command, env = calls[0]
    assert 'model_providers.ares.base_url="http://127.0.0.1:54321/v1"' in command
    assert 'model_reasoning_effort="low"' in command
    assert command[-4:] == ["-m", "local-model", "exec", "hi"]
    assert env is None
    assert not (tmp_path / "codex").exists()


def test_an_unready_server_is_not_started(profile, monkeypatch):
    monkeypatch.setattr(coder, "readiness", lambda spec: False)
    with pytest.raises(SystemExit, match="start the server first"):
        coder.main(["--config", str(profile), "claude"])


def test_codex_app_switches_the_default_and_restores_it(profile, calls, tmp_path, monkeypatch, capsys):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    original = 'model = "hosted-model"\neffort = "medium"\n\n[tools]\nweb = true\n'
    (home / "config.toml").write_text(original)
    for _ in range(2):
        assert coder.main(["--config", str(profile), "codex-app", "on"]) == 0
    switched = (home / "config.toml").read_text()
    assert switched.startswith('model = "local-model"\nmodel_provider = "ares"\neffort = "medium"\n')
    assert switched.count("[model_providers.ares]") == 1
    assert 'base_url = "http://127.0.0.1:54321/v1"' in switched
    assert "[tools]\nweb = true" in switched
    assert coder.main(["--config", str(profile), "codex-app", "off"]) == 0
    assert (home / "config.toml").read_text() == original
    with pytest.raises(SystemExit, match="nothing to restore"):
        coder.main(["--config", str(profile), "codex-app", "off"])


def test_codex_app_requires_one_switch(profile):
    with pytest.raises(SystemExit):
        coder.main(["--config", str(profile), "codex-app"])
