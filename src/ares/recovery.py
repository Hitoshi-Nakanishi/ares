"""Opt-in, bounded recovery after a driver fault, engine stall or process failure."""

import json
import math
import subprocess

from ares.supervisor import save_snapshot


def recovery_attempts(state, policy, now):
    path = state / "llm-recovery.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"attempts": []}
    attempts = data["attempts"]
    if not isinstance(attempts, list) or any(
        type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in attempts
    ):
        raise ValueError("Invalid recovery history")
    return [stamp for stamp in attempts if now - stamp < policy.window_seconds]


def recovery_blocker(state, pause, policy, now):
    reason = pause["reason"]
    recoverable = reason.startswith(
        (
            "GPU driver fault event ",
            "Engine made no progress for ",
            "Inference stopped answering for ",
            "Inference exited with code ",
            "Managed inference exited;",
        )
    )
    if not recoverable:
        return "operator or non-recoverable safety stop"
    attempts = recovery_attempts(state, policy, now)
    if len(attempts) >= policy.max_attempts:
        return "recovery attempt limit reached"
    stamp = pause["created_at"]
    if type(stamp) not in (int, float) or not math.isfinite(stamp):
        raise ValueError("Invalid pause timestamp")
    if now - max([stamp, *attempts]) < policy.cooldown_seconds:
        return "recovery cooldown"
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=uuid", "--format=csv,noheader"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if result.returncode or not result.stdout.strip().startswith("GPU-"):
        return "GPU driver is not responding"
    return None


def record_attempt(state, policy, now):
    attempts = recovery_attempts(state, policy, now)
    if not save_snapshot(state / "llm-recovery.json", {"attempts": [*attempts, now], "updated_at": now}):
        raise OSError("Cannot persist recovery attempt; refusing an unbounded restart")
