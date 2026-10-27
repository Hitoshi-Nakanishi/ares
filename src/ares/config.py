"""Validated local profiles; expand only the selected backend's environment values."""

import math
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import yaml

BACKENDS = ("strata", "vllm", "ollama", "sglang")


@dataclass(frozen=True)
class RecoveryPolicy:
    enabled: bool = False
    cooldown_seconds: int = 300
    window_seconds: int = 3600
    max_attempts: int = 3

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise TypeError("recovery.enabled must be a boolean")
        for name, value in asdict(self).items():
            if name != "enabled" and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
                raise ValueError(f"recovery.{name} must be a positive integer")
        if self.cooldown_seconds > self.window_seconds:
            raise ValueError("Recovery cooldown must fit inside its attempt window")


@dataclass(frozen=True)
class MemoryLimits:
    enforce_runtime: bool = True
    ram_gib: float = 32
    available_gib: float = 32
    startup_available_gib: float = 64
    free_commit_gib: float = 48
    job_commit_gib: float = 100

    def __post_init__(self):
        if not isinstance(self.enforce_runtime, bool):
            raise TypeError("memory.enforce_runtime must be a boolean")
        for name, value in asdict(self).items():
            if name == "enforce_runtime":
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"memory.{name} must be a positive finite number")
        if self.startup_available_gib < self.available_gib or self.job_commit_gib < self.ram_gib:
            raise ValueError("Startup headroom and job commit must cover their runtime limits")


def checked(mapping, allowed, location):
    if not isinstance(mapping, dict) or set(mapping) - set(allowed):
        raise ValueError(f"Unknown or invalid configuration fields in {location}")
    return mapping


def expand(value):
    if isinstance(value, str):

        def substitute(match):
            name = match.group(1)
            if not os.environ.get(name):
                raise ValueError(f"Set the environment variable {name}")
            return os.environ[name]

        return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", substitute, value)
    if isinstance(value, dict):
        return {key: expand(item) for key, item in value.items()}
    return value


def load_profile(path, backend=None, *, resolve_settings=True):
    path = Path(path).resolve()
    data = checked(
        yaml.safe_load(path.read_text(encoding="utf-8-sig")),
        ("version", "backend", "host", "port", "state_dir", "memory", "backends", "recovery"),
        "profile",
    )
    if data.get("version", 1) != 1 or data.get("host", "127.0.0.1") != "127.0.0.1":
        raise ValueError("Only version 1 and IPv4 loopback are supported")
    selected = backend or data.get("backend", "vllm")
    if selected not in BACKENDS:
        raise ValueError(f"backend must be one of {', '.join(BACKENDS)}")
    port = data.get("port", 54321)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    memory = MemoryLimits(**checked(data.get("memory", {}), (f.name for f in fields(MemoryLimits)), "memory"))
    recovery = RecoveryPolicy(**checked(data.get("recovery", {}), (f.name for f in fields(RecoveryPolicy)), "recovery"))
    if recovery.enabled and selected not in ("vllm", "sglang"):
        raise ValueError("Automatic recovery is supported only for vllm and sglang profiles")
    backends = checked(data.get("backends", {}), BACKENDS, "backends")
    settings = expand(backends.get(selected, {})) if resolve_settings else {}
    state_dir = Path(expand(data.get("state_dir", ".local"))).expanduser()
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    return {
        "backend": selected,
        "port": port,
        "state_dir": str(state_dir.resolve()),
        "memory": asdict(memory),
        "settings": settings,
        "recovery": asdict(recovery),
    }
