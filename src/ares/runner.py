"""Detached runner for a validated, private launch specification."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from ares.config import MemoryLimits
from ares.supervisor import supervise
from ares.watchdog import Watchdog


REMOVAL_SECONDS = 60


def container_owner(name):
    """The ``ares.owner`` label of the container named ``name``, or ``None`` when no such container exists."""
    inspection = subprocess.run(
        ["docker", "inspect", "--format", '{{index .Config.Labels "ares.owner"}}', name],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if inspection.returncode == 0:
        return inspection.stdout.strip()
    if "no such" in inspection.stderr.lower():
        return None
    raise RuntimeError("Cannot establish whether the managed Docker container stopped")


def wait_for_removal(name, timeout=REMOVAL_SECONDS):
    """Return once no container is named ``name``. ``docker stop`` returns before Docker removes a ``--rm``
    container, and a launch in that gap fails on the name still in use."""
    deadline = time.monotonic() + timeout
    while container_owner(name) is not None:
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Docker container {name} is still present after {timeout:.0f} seconds")
        time.sleep(1)


def stop_container(spec):
    if not spec.get("docker_name"):
        return
    owner = container_owner(spec["docker_name"])
    if owner is None:
        return
    if owner != spec.get("docker_owner"):
        raise RuntimeError("Refusing to stop an unrelated Docker container")
    subprocess.run(
        ["docker", "stop", "--time", "10", spec["docker_name"]], check=True, stdout=subprocess.DEVNULL, timeout=30
    )
    wait_for_removal(spec["docker_name"])


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
