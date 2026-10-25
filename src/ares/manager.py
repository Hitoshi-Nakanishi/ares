"""One selected backend at a time, with explicit configuration changes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from ares.backends import build_spec
from ares.config import load_profile
from ares.inhibition import inhibit
from ares.lifecycle import claim_start, server_environment, wait_for_exit
from ares.resources import require_memory_headroom
from ares.runner import stop_container


def owned_process(record):
    try:
        process = psutil.Process(record["pid"])
        command = process.cmdline()
        spec_path = str(Path(record["spec_path"]).resolve())
        if abs(process.create_time() - record["created"]) < 1 and "ares.runner" in command and spec_path in command:
            return process
    except (KeyError, OSError, psutil.Error):
        pass
    return None


def readiness(spec):
    base = f"http://127.0.0.1:{spec['port']}"
    with httpx.Client(timeout=5, trust_env=False) as client:
        if spec["backend"] == "strata":
            health = client.get(base + "/health")
            health.raise_for_status()
            if health.json().get("loaded") is not True:
                return False
        elif spec["backend"] == "vllm":
            client.get(base + "/health").raise_for_status()
        else:
            client.get(base + "/api/version").raise_for_status()
        models = client.get(base + "/v1/models")
        models.raise_for_status()
        return spec["model"] in [row.get("id") for row in models.json().get("data", [])]


def stop(record):
    process = owned_process(record)
    if process is None:
        if record and psutil.pid_exists(record.get("pid", -1)):
            raise SystemExit("Saved process ownership cannot be verified; inspect before stopping")
        if record:
            stop_container(record["spec"])
        return
    stop_container(record["spec"])
    children = process.children(recursive=True)
    for member in [*reversed(children), process]:
        try:
            member.terminate()
        except psutil.NoSuchProcess:
            pass
    alive = wait_for_exit([*children, process], timeout=15)
    for member in alive:
        member.kill()
    if wait_for_exit(alive, timeout=10):
        raise SystemExit("The previous server tree did not stop")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("start", "autostart", "stop", "restart", "status", "plan"), nargs="?", default="start"
    )
    parser.add_argument("--config", type=Path, default=os.getenv("LOCAL_LLM_CONFIG"))
    parser.add_argument("--backend", choices=("strata", "vllm", "ollama"))
    parser.add_argument("--wait", type=float, default=7200)
    parser.add_argument(
        "--resume", action="store_true", help="Explicitly clear an operator or safety stop before starting"
    )
    args = parser.parse_args(argv)
    if args.resume and args.action not in {"start", "restart"}:
        parser.error("--resume requires explicit start or restart")
    autostart = args.action == "autostart"
    if autostart:
        args.action = "start"
    if args.config is None:
        if autostart:
            print("Ares autostart skipped: no YAML profile configured")
            return 0
        raise SystemExit("Set LOCAL_LLM_CONFIG or pass --config with a private YAML profile")
    profile = load_profile(args.config, args.backend, resolve_settings=args.action != "stop")
    spec = build_spec(profile) if args.action != "stop" else None
    state = Path(profile["state_dir"])
    if args.action == "plan":
        print(json.dumps(spec, indent=2))
        return 0
    state.mkdir(parents=True, exist_ok=True)
    if args.action in ("start", "stop", "restart") and not claim_start(state / "llm-server.starting.json"):
        raise SystemExit("Local LLM lifecycle operation already in progress")
    pid_file = state / "llm-server.pid.json"
    record = json.loads(pid_file.read_text()) if pid_file.exists() else {}
    fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
    process = owned_process(record) if record else None
    pause = state / "llm-paused.json"
    if args.action == "stop":
        inhibit(state, "Operator stopped inference; explicit start --resume required")
    if args.action in {"start", "restart"}:
        if (
            profile["memory"]["enforce_runtime"]
            and record
            and process is None
            and not psutil.pid_exists(record.get("pid", -1))
            and not pause.exists()
        ):
            inhibit(state, "Managed inference exited; inspect logs before explicit start --resume")
        if pause.exists() and not args.resume:
            if autostart:
                print(f"Ares autostart skipped: inference is paused ({pause})")
                return 0
            raise SystemExit(f"Inference is paused; inspect {pause} and use start --resume explicitly")
    if args.action in ("stop", "restart"):
        stop(record)
        pid_file.unlink(missing_ok=True)
        process = None
        record = {}
    if args.action == "stop":
        print("Local LLM server stopped")
        return 0
    if record and process is None:
        # An exited PID is reusable; a live PID with unverifiable ownership is not.
        if psutil.pid_exists(record.get("pid", -1)):
            raise SystemExit("Saved server identity changed; inspect it before starting")
        pid_file.unlink(missing_ok=True)
    if process and record.get("fingerprint") != fingerprint:
        raise SystemExit("Backend or settings changed; use restart to apply the YAML profile")
    try:
        ready = readiness(spec)
    except (httpx.HTTPError, ValueError, KeyError):
        ready = False
    if args.action == "status":
        print(
            json.dumps(
                {
                    "backend": spec["backend"],
                    "model": spec["model"],
                    "ready": bool(process and ready),
                    "paused": pause.exists(),
                }
            )
        )
        return 0 if process and ready else 1
    if process and ready:
        print(f"Local LLM ready: {spec['backend']} http://127.0.0.1:{spec['port']}/v1 model={spec['model']}")
        return 0
    if process is None:
        import socket

        with socket.socket() as connection:
            if connection.connect_ex(("127.0.0.1", spec["port"])) == 0:
                raise SystemExit("The configured port already belongs to another server; stop it explicitly")
        memory = profile["memory"]
        try:
            require_memory_headroom(
                available_gib=memory["startup_available_gib"],
                commit_gib=memory["job_commit_gib"] + memory["free_commit_gib"],
            )
        except MemoryError as exc:
            if autostart:
                print(f"Ares autostart skipped: {exc}")
                return 0
            raise SystemExit(str(exc)) from exc
        spec = build_spec(profile, materialize=True)
        spec_path = state / "llm-launch.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with (state / "llm-server.out.log").open("a", encoding="utf-8") as stream:
            if args.resume:
                pause.unlink(missing_ok=True)
            try:
                child = subprocess.Popen(
                    [sys.executable, "-u", "-m", "ares.runner", str(spec_path)],
                    env=server_environment(),
                    cwd=state,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    creationflags=(
                        subprocess.DETACHED_PROCESS
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                        | subprocess.CREATE_BREAKAWAY_FROM_JOB
                    )
                    if os.name == "nt"
                    else 0,
                    start_new_session=os.name != "nt",
                )
            except OSError:
                inhibit(state, "Inference launch failed; inspect logs before explicit start --resume")
                raise
        process = psutil.Process(child.pid)
        record = {
            "pid": child.pid,
            "created": process.create_time(),
            "spec_path": str(spec_path),
            "spec": spec,
            "fingerprint": fingerprint,
        }
        pid_file.write_text(json.dumps(record), encoding="utf-8")
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        if not process.is_running():
            raise SystemExit(f"Local LLM exited; read {state / 'llm-server.out.log'}")
        try:
            if readiness(spec):
                print(f"Local LLM ready: {spec['backend']} http://127.0.0.1:{spec['port']}/v1 model={spec['model']}")
                return 0
        except (httpx.HTTPError, ValueError, KeyError):
            pass
        time.sleep(3)
    raise SystemExit("Local LLM readiness timed out; no fallback was started")
