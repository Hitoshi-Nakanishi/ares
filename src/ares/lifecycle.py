"""Manage the installed native Strata server."""

from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from ares.resources import STARTUP_COMMIT_GIB, require_memory_headroom

DEFAULT_START_WAIT_SECONDS = 7200


def server_environment() -> dict[str, str]:
    """Allow enough time for the configured resident expert copy on first start."""
    environment = os.environ.copy()
    environment.setdefault("STRATA_ENGINE_READY_S", str(DEFAULT_START_WAIT_SECONDS))
    source = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source, environment.get("PYTHONPATH"))))
    return environment


def matches_server(process: psutil.Process, root: Path) -> bool:
    command = process.cmdline()
    entry_points = {
        "serve.server",
        "strata_server.supervisor",
        "ares.supervisor",
        "ares.supervisor",
        str(root / "serve" / "server.py"),
    }
    if not entry_points.intersection(command):
        return False
    try:
        return Path(process.cwd()).resolve() == root.resolve()
    except psutil.AccessDenied:
        # Windows can expose the command line while denying the PEB read for cwd.
        if "--config" not in command[:-1]:
            return False
        config = Path(command[command.index("--config") + 1])
        return config.is_absolute() and config.resolve().parent == root.resolve()


def server_process(pid_file: Path, root: Path) -> psutil.Process | None:
    """Reject stale PIDs belonging to a different program before stopping anything."""
    if not pid_file.exists():
        return None
    try:
        saved = json.loads(pid_file.read_text())
        process = psutil.Process(saved["pid"])
        if abs(process.create_time() - saved["created"]) < 1 and matches_server(process, root):
            return process
    except (OSError, ValueError, KeyError, psutil.Error):
        pass
    return None


def running_servers(root: Path) -> list[psutil.Process]:
    """Find already running Strata API servers when their PID record is stale."""
    matches = []
    for process in psutil.process_iter():
        try:
            if matches_server(process, root):
                matches.append(process)
        except (OSError, psutil.Error):
            continue
    matching_pids = {process.pid for process in matches}
    return [process for process in matches if process.ppid() not in matching_pids]


def wait_for_exit(processes: list[psutil.Process], timeout: float) -> list[psutil.Process]:
    try:
        _, alive = psutil.wait_procs(processes, timeout=timeout)
        return alive
    except psutil.AccessDenied:
        # SYNCHRONIZE access can be denied even when termination is permitted.
        deadline = time.monotonic() + timeout
        while True:
            alive = [process for process in processes if process.is_running()]
            if not alive or time.monotonic() >= deadline:
                return alive
            time.sleep(0.1)


def claim_start(lock_file: Path) -> bool:
    """Atomically claim a long first start, replacing only a stale claim."""
    process = psutil.Process()
    record = {"pid": process.pid, "created": process.create_time()}
    for _ in range(2):
        try:
            descriptor = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                saved = json.loads(lock_file.read_text())
                owner = psutil.Process(saved["pid"])
                if abs(owner.create_time() - saved["created"]) < 1:
                    return False
            except (OSError, ValueError, KeyError, psutil.Error):
                pass
            lock_file.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream)
        atexit.register(release_start, lock_file, record)
        return True
    raise SystemExit(f"Could not claim Strata start lock: {lock_file}")


def release_start(lock_file: Path, record: dict[str, float]) -> None:
    """Release only the start claim made by this process."""
    try:
        if json.loads(lock_file.read_text()) == record:
            lock_file.unlink()
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--config" in argv or os.getenv("LOCAL_LLM_CONFIG"):
        from ares.manager import main as managed_main

        return managed_main(argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "restart", "status"), default="start", nargs="?")
    parser.add_argument("--wait", type=float, default=DEFAULT_START_WAIT_SECONDS)
    args = parser.parse_args(argv)
    if not os.getenv("STRATA_HOME"):
        raise SystemExit("Set STRATA_HOME to the installed native Strata directory")
    root = Path(os.environ["STRATA_HOME"]).resolve()
    config = Path(os.getenv("STRATA_CONFIG", str(root / "strata.json")))
    if args.action in {"start", "restart"} and not claim_start(root / "strata-server.starting.json"):
        print("Strata start already in progress")
        return 0
    settings = json.loads(config.read_text(encoding="utf-8-sig"))
    base_url = f"http://127.0.0.1:{settings.get('port', 8000)}"
    headers = {"Authorization": f"Bearer {settings['api_key']}"} if settings.get("api_key") else {}
    pid_file = root / "strata-server.pid.json"
    process = server_process(pid_file, root)
    if process is None:
        matches = running_servers(root)
        if len(matches) > 1:
            raise SystemExit(f"Multiple Strata servers are running under {root}; stop the duplicate processes")
        if matches:
            process = matches[0]
            pid_file.write_text(json.dumps({"pid": process.pid, "created": process.create_time()}))
    if args.action in {"stop", "restart"} and process:
        children = process.children(recursive=True)
        for target in [*reversed(children), process]:
            try:
                target.terminate()
            except psutil.NoSuchProcess:
                pass
        alive = wait_for_exit([*children, process], timeout=15)
        for target in alive:
            target.kill()
        if remaining := wait_for_exit(alive, timeout=10):
            raise SystemExit(f"Strata processes did not stop: {[target.pid for target in remaining]}")
        pid_file.unlink(missing_ok=True)
        process = None
    if args.action == "stop":
        print("Strata stopped")
        return 0

    with httpx.Client(timeout=5, trust_env=False, headers=headers) as client:
        try:
            health = client.get(f"{base_url}/health")
            health.raise_for_status()
            state = health.json()
        except (httpx.HTTPError, ValueError):
            state = {}
        if state and state.get("model") != settings["model_name"]:
            raise SystemExit(f"Another server owns {base_url}: {state.get('model')}")
        models = []
        try:
            models_response = client.get(f"{base_url}/v1/models")
            models_response.raise_for_status()
            models = [entry.get("id") for entry in models_response.json().get("data", [])]
        except (httpx.HTTPError, ValueError):
            pass
        ready = state.get("loaded") is True and settings["model_name"] in models
        if args.action == "status":
            print(json.dumps(state or {"loaded": False}, ensure_ascii=False))
            return 0 if ready else 1
        if ready:
            if os.name == "nt" and (
                process is None
                or not {"ares.supervisor", "ares.supervisor"}.intersection(process.cmdline())
            ):
                raise SystemExit("Existing Strata has no RAM supervisor; use the managed restart before analysis")
            print(f"Strata ready: {base_url}/v1 model={state['model']}")
            return 0
        if process is None:
            try:
                require_memory_headroom(available_gib=64, commit_gib=STARTUP_COMMIT_GIB)
            except MemoryError as exc:
                raise SystemExit(str(exc)) from exc
            python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = (
                [sys.executable, "-u", "-m", "ares.supervisor"]
                if os.name == "nt"
                else [str(python), "-u", "-m", "serve.server", "--engine", "strata", "--host", "127.0.0.1"]
            )
            with (root / "strata-server.out.log").open("a", encoding="utf-8") as log:
                child = subprocess.Popen(
                    [
                        *command,
                        "--config",
                        str(config),
                        "--port",
                        str(settings.get("port", 8000)),
                    ],
                    cwd=root,
                    env=server_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
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
            process = psutil.Process(child.pid)
            pid_file.write_text(json.dumps({"pid": child.pid, "created": process.create_time()}))
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            if not process.is_running():
                raise SystemExit(f"Strata exited; read {root / 'strata-server.out.log'}")
            try:
                health = client.get(f"{base_url}/health")
                health.raise_for_status()
                state = health.json()
                models_response = client.get(f"{base_url}/v1/models")
                models_response.raise_for_status()
                models = [entry.get("id") for entry in models_response.json().get("data", [])]
                if (
                    state.get("loaded")
                    and state.get("model") == settings["model_name"]
                    and settings["model_name"] in models
                ):
                    print(f"Strata ready: {base_url}/v1 model={state['model']}")
                    return 0
            except (httpx.HTTPError, ValueError):
                pass
            time.sleep(3)
    raise SystemExit(f"Strata not ready within {args.wait:g}s; read {root / 'strata-server.out.log'}")


if __name__ == "__main__":
    raise SystemExit(main())
