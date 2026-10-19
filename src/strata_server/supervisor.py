"""Run native Strata with independent physical-RAM and commit protection."""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

import psutil

from strata_server.resources import JOB_COMMIT_GIB, STARTUP_COMMIT_GIB, require_memory_headroom
from strata_server.windows_memory import WindowsMemoryJob

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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args(argv)
    if os.name != "nt":
        raise SystemExit("The bounded native supervisor requires Windows")
    root = args.config.resolve().parent
    require_memory_headroom(available_gib=64, commit_gib=STARTUP_COMMIT_GIB)
    job = WindowsMemoryJob()
    child = None
    snapshot = {"status": "starting", "ram_limit_gib": RAM_LIMIT_GIB, "job_commit_limit_gib": JOB_COMMIT_GIB}
    record = root / "strata-memory.json"
    peak_rss = peak_commit = 0
    last_snapshot_warning = float("-inf")
    result = 1
    try:
        child = subprocess.Popen(
            [
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
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            creationflags=0x00000004,
        )
        job.resume(child)
        process = psutil.Process(child.pid)
        while child.poll() is None:
            usage = memory_snapshot(process)
            peak_rss = max(peak_rss, usage["rss"])
            peak_commit = max(peak_commit, usage["commit"])
            snapshot.update(usage, status="running", updated_at=time.time(), peak_rss=peak_rss, peak_commit=peak_commit)
            if usage["rss"] > RAM_LIMIT_GIB * 2**30:
                raise MemoryError(f"Strata physical RAM exceeded {RAM_LIMIT_GIB} GiB")
            require_memory_headroom(available_gib=32, commit_gib=48)
            if not save_snapshot(record, snapshot) and time.monotonic() - last_snapshot_warning >= 30:
                print("Strata memory snapshot unavailable; live memory limits remain active", flush=True)
                last_snapshot_warning = time.monotonic()
            time.sleep(0.5)
        result = child.returncode
        snapshot["status"] = "exited"
        snapshot["exit_code"] = result
    except (MemoryError, OSError, RuntimeError, psutil.Error) as exc:
        snapshot.update(status="stopped", error=str(exc))
        print(f"Strata supervisor stopped inference: {exc}", flush=True)
    finally:
        job.close()
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=15)
        snapshot["updated_at"] = time.time()
        save_snapshot(record, snapshot)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
