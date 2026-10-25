"""Repeatable smoke measurements against the explicitly selected, running backend."""

import argparse
import csv
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psutil

from ares.backends import build_spec
from ares.config import BACKENDS, load_profile
from ares.manager import owned_process, readiness

CASES = (
    ("arithmetic", "What is 1+1? Reply with the number only.", "2"),
    ("instruction", "Reply exactly OK", "OK"),
    ("extraction", "Revenue was 123 million. Reply with the revenue number only.", "123"),
)


def measure(spec, *, repetitions=3, timeout=120):
    rows = []
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        for repeat in range(repetitions):
            for case_id, prompt, expected in CASES:
                started = time.monotonic()
                row = {
                    "backend": spec["backend"],
                    "model": spec["model"],
                    "repeat": repeat + 1,
                    "case": case_id,
                    "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "time_utc": datetime.now(UTC).isoformat(),
                    "completion_tokens": "",
                    "correct": False,
                    "tokens_per_second": "",
                    "finish_reason": "",
                    "status": "failed",
                    "peak_process_tree_ram_gib": "",
                    "host_available_ram_gib": "",
                }
                try:
                    response = client.post(
                        f"http://127.0.0.1:{spec['port']}/v1/chat/completions",
                        json={
                            "model": spec["model"],
                            "messages": [{"role": "user", "content": prompt}],
                            "max_tokens": 16,
                            "temperature": 0,
                            "stream": False,
                            **(
                                {"chat_template_kwargs": {"enable_thinking": False}}
                                if spec["backend"] in ("strata", "sglang")
                                else {}
                            ),
                        },
                    )
                    response.raise_for_status()
                    result = response.json()
                    choice = result["choices"][0]
                    row.update(
                        correct=choice["message"]["content"].strip() == expected,
                        finish_reason=choice.get("finish_reason", ""),
                        status="ok",
                    )
                    tokens = result.get("usage", {}).get("completion_tokens")
                    if isinstance(tokens, int) and tokens > 0:
                        row["completion_tokens"] = tokens
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                    pass
                row["elapsed_seconds"] = round(time.monotonic() - started, 4)
                if row["completion_tokens"] and row["elapsed_seconds"] > 0:
                    row["tokens_per_second"] = round(row["completion_tokens"] / row["elapsed_seconds"], 3)
                row["host_available_ram_gib"] = round(psutil.virtual_memory().available / 2**30, 3)
                if spec.get("state_dir"):
                    try:
                        memory = json.loads((Path(spec["state_dir"]) / "llm-memory.json").read_text())
                        row["peak_process_tree_ram_gib"] = round(memory["peak_rss"] / 2**30, 3)
                    except (OSError, ValueError, KeyError):
                        pass
                rows.append(row)
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=os.getenv("LOCAL_LLM_CONFIG"), required=not os.getenv("LOCAL_LLM_CONFIG")
    )
    parser.add_argument("--backend", choices=BACKENDS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("repetitions must be positive")
    spec = build_spec(load_profile(args.config, args.backend))
    record = json.loads((Path(spec["state_dir"]) / "llm-server.pid.json").read_text())
    if (
        not owned_process(record)
        or record["spec"]["backend"] != spec["backend"]
        or record["spec"]["model"] != spec["model"]
        or not readiness(spec)
    ):
        raise SystemExit("Start the exact selected backend and model before measuring; no engine will be started here")
    rows = measure(spec, repetitions=args.repetitions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "backend": spec["backend"],
                "requests": len(rows),
                "correct": sum(row["correct"] for row in rows),
                "failed": sum(row["status"] != "ok" for row in rows),
                "output": str(args.output),
            }
        )
    )
    return 0 if all(row["status"] == "ok" and row["correct"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
