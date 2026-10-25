"""Run native Strata with independent physical-RAM and commit protection."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import psutil

from ares.config import MemoryLimits
from ares.inhibition import inhibit
from ares.resources import require_memory_headroom
from ares.watchdog import Watchdog
from ares.windows_memory import WindowsMemoryJob

RAM_LIMIT_GIB = 32


def memory_snapshot(process):
    processes = [process, *process.children(recursive=True)]
    rows = []
    for member in processes:
        try:
            memory = member.memory_info()
            rows.append({"pid": member.pid, "name": member.name(), "rss": memory.rss, "commit": memory.vms})
        except psutil.NoSuchProcess:
            continue
    return {"rss": sum(row["rss"] for row in rows), "commit": sum(row["commit"] for row in rows), "processes": rows}


def save_snapshot(record, snapshot):
    temporary = record.with_name(f"{record.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(snapshot), encoding="utf-8")
        for attempt in range(5):
            try:
                temporary.replace(record)
                return True
            except PermissionError:
                time.sleep(0.05 * (attempt + 1))
    except OSError:
        pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return False


def supervise(command, cwd, record, *, limits=None, environment=None, label="Strata", watchdog=None):
    limits = limits or MemoryLimits()
    job = None
    child = None
    snapshot = {
        "status": "starting",
        "enforce_runtime": limits.enforce_runtime,
        "ram_limit_gib": limits.ram_gib if limits.enforce_runtime else None,
        "job_commit_limit_gib": limits.job_commit_gib if limits.enforce_runtime else None,
    }
    peak_rss = peak_commit = 0
    last_snapshot_warning = float("-inf")
    result = 1
    try:
        require_memory_headroom(
            available_gib=limits.startup_available_gib, commit_gib=limits.job_commit_gib + limits.free_commit_gib
        )
        job = WindowsMemoryJob(commit_gib=limits.job_commit_gib) if os.name == "nt" and limits.enforce_runtime else None
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            creationflags=0x00000004 if job is not None else 0,
            start_new_session=os.name != "nt",
        )
        if job is not None:
            job.resume(child)
        if not limits.enforce_runtime:
            snapshot.update(status="running", pid=child.pid, updated_at=time.time())
            save_snapshot(record, snapshot)
            if watchdog is None:
                result = child.wait()
                snapshot.update(status="exited", exit_code=result)
                return result
        process = psutil.Process(child.pid) if limits.enforce_runtime else None
        watched_at = time.monotonic()
        while child.poll() is None:
            if process is not None:
                usage = memory_snapshot(process)
                peak_rss = max(peak_rss, usage["rss"])
                peak_commit = max(peak_commit, usage["commit"])
                snapshot.update(
                    usage, status="running", updated_at=time.time(), peak_rss=peak_rss, peak_commit=peak_commit
                )
                if usage["rss"] > limits.ram_gib * 2**30:
                    raise MemoryError(f"{label} physical RAM exceeded {limits.ram_gib} GiB")
                snapshot["host_memory"] = require_memory_headroom(
                    available_gib=limits.available_gib, commit_gib=limits.free_commit_gib
                )
                if not save_snapshot(record, snapshot) and time.monotonic() - last_snapshot_warning >= 30:
                    print(f"{label} memory snapshot unavailable; live memory limits remain active", flush=True)
                    last_snapshot_warning = time.monotonic()
            if watchdog is not None and time.monotonic() - watched_at >= watchdog.interval:
                watchdog.check()
                watched_at = time.monotonic()
            time.sleep(0.5)
        result = child.returncode
        snapshot["status"] = "exited"
        snapshot["exit_code"] = result
    except (MemoryError, OSError, RuntimeError, psutil.Error) as exc:
        snapshot.update(status="stopped", error=str(exc))
        print(f"{label} supervisor stopped inference: {exc}", flush=True)
    finally:
        if result != 0 and (limits.enforce_runtime or child is None or "error" in snapshot):
            try:
                inhibit(record.parent, snapshot.get("error", f"Inference exited with code {result}"))
            except OSError as exc:
                print(f"{label} safety stop could not be persisted: {exc}", flush=True)
        if job is not None:
            job.close()
        if child is not None:
            if child.poll() is None:
                if os.name != "nt":
                    for member in reversed(psutil.Process(child.pid).children(recursive=True)):
                        try:
                            member.kill()
                        except psutil.NoSuchProcess:
                            pass
                child.kill()
            child.wait(timeout=15)
        snapshot["updated_at"] = time.time()
        save_snapshot(record, snapshot)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("The legacy native supervisor requires Windows; use a YAML profile")
    root = args.config.resolve().parent
    command = [
        str(root / ".venv/Scripts/python.exe"),
        "-u",
        "-m",
        "serve.server",
        "--engine",
        "strata",
        "--host",
        "127.0.0.1",
        "--config",
        str(args.config.resolve()),
        "--port",
        str(args.port),
    ]
    return supervise(command, root, root / "strata-memory.json", watchdog=Watchdog())


if __name__ == "__main__":
    raise SystemExit(main())
