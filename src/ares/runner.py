"""Detached runner for a validated, private launch specification."""

import json
import os
import subprocess
import sys
from pathlib import Path

from ares.config import MemoryLimits
from ares.supervisor import supervise
from ares.watchdog import Watchdog


def stop_container(spec):
    if not spec.get("docker_name"):
        return
    inspection = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "ares.owner"}}', spec["docker_name"]],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if inspection.returncode == 0:
        if inspection.stdout.strip() != spec.get("docker_owner"):
            raise RuntimeError("Refusing to stop an unrelated Docker container")
        subprocess.run(
            ["docker", "stop", "--time", "10", spec["docker_name"]], check=True, stdout=subprocess.DEVNULL, timeout=30
        )
    elif "no such" not in inspection.stderr.lower():
        raise RuntimeError("Cannot establish whether the managed Docker container stopped")


def main():
    spec = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    environment = os.environ.copy()
    environment.update(spec["env"])
    try:
        return supervise(
            spec["command"],
            spec["cwd"],
            Path(spec["state_dir"]) / "llm-memory.json",
            limits=MemoryLimits(**spec["memory"]),
            environment=environment,
            label=spec["backend"],
            watchdog=Watchdog(spec["port"] if spec["backend"] in ("vllm", "sglang") else None),
        )
    finally:
        stop_container(spec)


if __name__ == "__main__":
    raise SystemExit(main())
