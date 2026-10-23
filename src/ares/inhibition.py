"""Persist operator and safety stops independently of telemetry snapshots."""

import json
import os
import time
from pathlib import Path


def inhibit(state: Path, reason: str) -> None:
    state.mkdir(parents=True, exist_ok=True)
    target = state / "llm-paused.json"
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps({"reason": reason, "created_at": time.time()}), encoding="utf-8")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
