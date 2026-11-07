import json

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


def test_opencode_app_adds_and_removes_only_its_provider(profile, calls, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = tmp_path / "xdg" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    other = {"name": "Other", "models": {"m": {}}}
    config.write_text(json.dumps({"model": "ares/old", "providers": {"other": other}, "theme": "dark"}))
    assert coder.main(["--config", str(profile), "opencode-app", "on"]) == 0
    switched = json.loads(config.read_text())
    assert switched["providers"]["other"] == other
    assert switched["providers"]["ares"]["settings"]["baseURL"] == "http://127.0.0.1:54321/v1"
    assert list(switched["providers"]["ares"]["models"]) == ["local-model"]
    assert coder.main(["--config", str(profile), "opencode-app", "off"]) == 0
    assert json.loads(config.read_text()) == {"providers": {"other": other}, "theme": "dark"}


def test_opencode_app_leaves_a_commented_config_alone(profile, calls, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config = tmp_path / "opencode" / "opencode.json"
    config.parent.mkdir()
    config.write_text('{\n  // mine\n  "theme": "dark"\n}\n')
    with pytest.raises(SystemExit, match="not plain JSON"):
        coder.main(["--config", str(profile), "opencode-app", "on"])
    assert "// mine" in config.read_text()


def test_install_opencode_uses_the_pinned_winget_package(calls, monkeypatch):
    monkeypatch.setattr(coder.os, "name", "nt")
    assert coder.main(["install", "opencode"]) == 0
    assert calls[0][0][:5] == ["winget", "install", "--id", "Anomaly.OpenCode.Desktop", "--exact"]


def test_codex_app_requires_one_switch(profile):
    with pytest.raises(SystemExit):
        coder.main(["--config", str(profile), "codex-app"])
