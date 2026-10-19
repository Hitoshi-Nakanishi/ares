import json
import os
import socket
import subprocess
import sys

import pytest
import yaml


@pytest.mark.skipif(os.name != "nt", reason="Small real Windows job lifecycle")
def test_yaml_launch_restart_requirement_and_stop_without_installed_models(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    state = tmp_path / "state"
    state.mkdir()
    # Python's script argument occupies the same position as `ollama serve`.
    (state / "serve").write_text("""import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"data": [{"id": "dummy-model"}]} if self.path == "/v1/models" else {"version": "test"}
        payload = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
HTTPServer(("127.0.0.1", int(os.environ["OLLAMA_HOST"].rsplit(":",1)[1])), Handler).serve_forever()
""")
    data = {
        "state_dir": str(state),
        "port": port,
        "backend": "ollama",
        "memory": {
            "ram_gib": 0.125,
            "available_gib": 1,
            "startup_available_gib": 1,
            "free_commit_gib": 1,
            "job_commit_gib": 0.25,
        },
        "backends": {
            "ollama": {"executable": sys.executable, "model": "dummy-model"},
            "vllm": {"model_path": "${UNSET_OFFLINE_MODEL}"},
        },
    }
    profile = tmp_path / "private.yaml"
    profile.write_text(yaml.safe_dump(data))

    def command(action):
        return subprocess.run(
            [sys.executable, "-m", "ares", action, "--config", str(profile), "--wait", "10"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )

    try:
        started = command("start")
        assert started.returncode == 0, started.stderr + started.stdout
        before = json.loads((state / "llm-server.pid.json").read_text())["pid"]
        assert command("status").returncode == 0
        data["memory"]["ram_gib"] = 0.1
        profile.write_text(yaml.safe_dump(data))
        changed = command("start")
        assert changed.returncode != 0 and "restart" in changed.stderr
        assert json.loads((state / "llm-server.pid.json").read_text())["pid"] == before
        data["backend"] = "vllm"
        profile.write_text(yaml.safe_dump(data))
        stopped = command("stop")
        assert stopped.returncode == 0, stopped.stderr
        assert not (state / "llm-server.pid.json").exists()
    finally:
        stopped = command("stop")
        assert stopped.returncode == 0, stopped.stderr
    with socket.socket() as connection:
        assert connection.connect_ex(("127.0.0.1", port)) != 0
