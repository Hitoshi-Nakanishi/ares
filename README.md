# Strata local server

A small launcher for an independently installed native Strata GPU API. It manages one server process tree, checks the loaded
model, and protects memory headroom for other applications. It does not include an inference engine, model weights, model
downloads, application workflows, databases, or credentials. The existing native engine provides the HTTP API.

## Install and run

Use Python 3.11 or newer on Windows. Install native Strata separately and provision its model before starting this launcher.

```powershell
python -m pip install -e .
$env:STRATA_HOME = '<native-strata-directory>'
$env:STRATA_CONFIG = '<private-native-config.json>'
python -m strata_server start
python -m strata_server status
python -m strata_server stop
```

The native installation needs its own `.venv/Scripts/python.exe` with the `serve.server` module and engine dependencies. The
launcher environment needs only this package's dependencies. `STRATA_CONFIG` must be an existing native Strata configuration
containing `model_name`, the engine command, tokenizer and model settings. Its default is `strata.json` under `STRATA_HOME`.
The example shows connection fields only; it is not a complete engine configuration. Keep the real configuration outside this
repository. Nothing downloads models or installs the native engine on demand.

The API binds explicitly to `127.0.0.1`, even when the native config requests a broader host. The native port defaults to 8000
when absent from that config. Clients use `http://127.0.0.1:<port>/v1/chat/completions`. No LAN listener, firewall opening,
port forwarding, Tailscale publishing, tunnel, boot task, or automatic restart is installed. `start` is idempotent and checks
both `/health` and the exact configured model in `/v1/models`. `restart` stops the managed tree first. A health check is not a
real inference or persistence test.

## Memory and lifetime

The Windows supervisor starts the native server suspended, assigns it to a kill-on-close job, then resumes it. Children inherit
a 100 GiB aggregate **commit** limit, including Windows GPU backing; this is not a physical-RAM allowance. Startup requires
64 GiB available physical RAM and 148 GiB free host commit. Every 0.5 seconds the supervisor checks the process tree and stops
it above 32 GiB physical RAM, below 32 GiB host-available RAM, or below 48 GiB free host commit. The physical cutoff is sampled,
not an OS-enforced working-set cap. These conservative defaults target a large-memory Windows workstation and may refuse to
start on smaller hosts. GPU allocation remains in the separate native config; the launcher does not increase GPU use.

The root supervisor detaches from the invoking execution job. Its inference children remain in its bounded job. PID records
include creation time and process identity. Temporary telemetry reader locks cannot kill healthy inference. Runtime PID,
lock, log and memory files are written under `STRATA_HOME`, not into version control. This preserves other applications'
headroom, but it cannot guarantee against host-wide memory exhaustion or replace workload validation.

## Development and distribution

```powershell
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

Tests use small processes and mocked HTTP responses; no GPU, model, database, account or network credential is needed. Native
Windows job tests skip on other operating systems. The actual protected launcher is verified on Windows; Linux memory
protection and GPU performance are not claimed.

Only the source, tests, packaging metadata, this README and a generic example belong in Git. The allowlist ignore file excludes
unlisted files by default, including private configuration, `.env`, logs, models, virtual environments and build products.
Review the staged files and complete history before any future publication; an ignore rule cannot sanitize an already tracked
file. Engine redistribution and model licensing are separate from this launcher. No external remote is configured by setup.
