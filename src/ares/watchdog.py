"""Stop inference when the GPU driver faults or a serving engine stops making progress."""

import os
import re
import subprocess
import time

import httpx

CHECK_SECONDS = 15
STALL_SECONDS = 300
# NVIDIA driver errors and Windows display-driver timeouts leave a CUDA engine spinning on a GPU it can no longer use.
DRIVER_FAULTS = (
    "*[System[(Provider[@Name='nvlddmkm'] and (EventID=13 or EventID=14 or EventID=153))"
    " or (Provider[@Name='Display'] and EventID=4101)]]"
)
RUNNING = {"vllm:num_requests_running", "sglang:num_running_reqs"}
PROGRESS = {
    "vllm:iteration_tokens_total_count",
    "vllm:prompt_tokens_total",
    "vllm:generation_tokens_total",
    "sglang:prompt_tokens_total",
    "sglang:generation_tokens_total",
}
SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}]*\})?\s+(\S+)")


def latest_driver_fault():
    """Return (record number, event id, UTC time) of the newest GPU driver fault, or None when there is none.

    Raises OSError when the Windows System log cannot be read.
    """
    if os.name != "nt":
        raise OSError("GPU driver events are read from the Windows System log")
    try:
        result = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{DRIVER_FAULTS}", "/rd:true", "/c:1", "/f:xml"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise OSError("The Windows System log query timed out") from exc
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"wevtutil exited with {result.returncode}")
    record = re.search(r"<EventRecordID>(\d+)</EventRecordID>", result.stdout)
    if record is None:
        return None
    event = re.search(r"<EventID[^>]*>(\d+)</EventID>", result.stdout)
    created = re.search(r"SystemTime='([^']+)'", result.stdout)
    return int(record.group(1)), event.group(1) if event else "", created.group(1) if created else ""


def engine_metrics(port):
    """Sum the running-request and progress samples a vLLM or SGLang server exposes; {} when it exposes none."""
    response = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=10, trust_env=False)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    totals = {}
    for line in response.text.splitlines():
        match = SAMPLE.match(line)
        if match and match.group(1) in RUNNING | PROGRESS:
            try:
                totals[match.group(1)] = totals.get(match.group(1), 0.0) + float(match.group(2))
            except ValueError:
                continue
    return totals


class Watchdog:
    """Raise RuntimeError once inference holds a faulted GPU or an engine that no longer finishes work.

    Driver faults count only after construction, so build the watchdog before launching the server. Progress is
    checked only when a port is given: the engine stalls when requests are running and no step, prompt or output
    token completes for STALL_SECONDS, or stops answering for that long after it first answered. Unreadable
    telemetry never stops inference by itself.
    """

    interval = CHECK_SECONDS

    def __init__(self, port=None):
        self.port = port
        try:
            fault = latest_driver_fault()
        except OSError as exc:
            print(f"GPU driver events unavailable; driver fault stops are off: {exc}", flush=True)
            self.baseline = None
        else:
            self.baseline = fault[0] if fault else 0
        self.answered = self.progressed = self.progress = None

    def check(self):
        if self.baseline is not None:
            try:
                fault = latest_driver_fault()
            except OSError:
                fault = None
            if fault and fault[0] > self.baseline:
                raise RuntimeError(
                    f"GPU driver fault event {fault[1]} at {fault[2]}; inference stopped to free the GPU"
                )
        if self.port is None:
            return
        now = time.monotonic()
        try:
            metrics = engine_metrics(self.port)
        except httpx.HTTPError:
            if self.answered is not None and now - self.answered >= STALL_SECONDS:
                raise RuntimeError(f"Inference stopped answering for {now - self.answered:.0f} s") from None
            return
        self.answered = now
        running = sum(value for name, value in metrics.items() if name in RUNNING)
        progress = sum(value for name, value in metrics.items() if name in PROGRESS)
        if not running or self.progressed is None or progress != self.progress:
            self.progress, self.progressed = progress, now
        elif now - self.progressed >= STALL_SECONDS:
            raise RuntimeError(
                f"Engine made no progress for {now - self.progressed:.0f} s with {running:g} requests running"
            )
