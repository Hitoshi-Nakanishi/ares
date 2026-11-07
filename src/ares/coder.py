"""Run coding agents on the server a profile describes; this command never starts or stops the server.

`claude` and `codex` start one agent session with the served model, `codex-app on` makes it the Codex default (the Codex app
reads only that) and `codex-app off` restores the default saved by `on`.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

from ares.backends import build_spec
from ares.config import load_profile
from ares.manager import readiness

PROVIDER = "ares"
DEFAULT_KEY = re.compile(r"\s*(model|model_provider)\s*=")
TABLE = re.compile(r"\s*\[")


def served(config):
    profile = load_profile(config)
    spec = build_spec(profile)
    try:
        ready = readiness(spec)
    except (httpx.HTTPError, ValueError, KeyError):
        ready = False
    if not ready:
        raise SystemExit(f"{spec['model']} is not ready on port {spec['port']}; start the server first")
    return profile, spec


def run(command, env=None):
    executable = shutil.which(command[0])
    if executable is None:
        raise SystemExit(f"{command[0]} is not on PATH")
    return subprocess.call([executable, *command[1:]], env=env)


def claude(spec, effort, args):
    model = spec["model"]
    env = os.environ | {
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{spec['port']}",
        "ANTHROPIC_AUTH_TOKEN": "local",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_SMALL_FAST_MODEL": model,
    }
    return run(["claude", "--effort", effort, *args], env)


def provider_overrides(spec):
    return [
        f'model_provider="{PROVIDER}"',
        f'model_providers.{PROVIDER}.name="Ares"',
        f'model_providers.{PROVIDER}.base_url="http://127.0.0.1:{spec["port"]}/v1"',
        f'model_providers.{PROVIDER}.wire_api="responses"',
    ]


def codex(spec, effort, args):
    overrides = [*provider_overrides(spec), f'model_reasoning_effort="{effort}"']
    return run(["codex", *(item for value in overrides for item in ("-c", value)), "-m", spec["model"], *args])


def split_config(text):
    lines = text.splitlines()
    first = next((index for index, line in enumerate(lines) if TABLE.match(line)), len(lines))
    return lines[:first], lines[first:]


def without_table(lines, header):
    kept, skipping = [], False
    for line in lines:
        if TABLE.match(line):
            skipping = line.strip() == header
        if not skipping:
            kept.append(line)
    return kept


def codex_app(spec, state, switch):
    config = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
    saved = state / "codex-default.json"
    top, tables = split_config(config.read_text(encoding="utf-8") if config.exists() else "")
    current = [line for line in top if DEFAULT_KEY.match(line)]
    rest = [line for line in top if not DEFAULT_KEY.match(line)]
    header = f"[model_providers.{PROVIDER}]"
    if switch == "status":
        print("\n".join(current) or "Codex uses its built-in default")
        return 0
    if switch == "on":
        if not any(f'"{PROVIDER}"' in line for line in current):
            saved.write_text(json.dumps(current), encoding="utf-8")
        chosen = [f'model = "{spec["model"]}"', f'model_provider = "{PROVIDER}"']
        table = [header, 'name = "Ares"', f'base_url = "http://127.0.0.1:{spec["port"]}/v1"', 'wire_api = "responses"']
        tables = [*without_table(tables, header), "", *table]
    else:
        if not saved.exists():
            raise SystemExit(f"No saved Codex default in {saved}; nothing to restore")
        chosen = json.loads(saved.read_text(encoding="utf-8"))
        tables = without_table(tables, header)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join([*chosen, *rest, *tables]).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    if switch == "off":
        saved.unlink()
    print(f"Codex default {switch}: {', '.join(chosen) or 'built-in'}; restart the Codex app to apply it")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ares coder",
        description=__doc__,
        epilog="Options go before the agent; everything after it is passed to the agent unchanged.",
    )
    parser.add_argument("--config", type=Path, default=os.getenv("LOCAL_LLM_CONFIG"))
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("agent", choices=("claude", "codex", "codex-app"))
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.config is None:
        raise SystemExit("Set LOCAL_LLM_CONFIG or pass --config with a private YAML profile")
    if args.agent == "codex-app":
        if args.rest not in (["on"], ["off"], ["status"]):
            parser.error("codex-app takes exactly one of on, off or status")
        switch = args.rest[0]
        if switch != "on":
            profile = load_profile(args.config)
            return codex_app(build_spec(profile), Path(profile["state_dir"]), switch)
        profile, spec = served(args.config)
        return codex_app(spec, Path(profile["state_dir"]), switch)
    _, spec = served(args.config)
    if args.agent == "claude":
        return claude(spec, args.effort, args.rest)
    return codex(spec, args.effort, args.rest)
