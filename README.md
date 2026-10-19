# Local LLM Server

Local-only launch and memory supervision for **Strata (default)**, **vLLM**, and **Ollama**. This is server management, not an
agent framework or inference engine. Model weights, credentials, application workflows and databases are excluded. There is no
automatic fallback, model download, external listener or boot task. Strata and Ollama run natively on Windows; vLLM uses an
already installed GPU Docker image or a native Linux installation.

## Install and run

Use Python 3.11 or newer. Install the selected engine and model separately. Copy `examples/server.example.yaml` to an ignored
or external private location. Set its paths and model names, or supply its `${VARIABLE}` values through the environment.

```powershell
python -m pip install -e .
python -m ares start --config private/server.yaml
python -m ares status --config private/server.yaml
python -m ares stop --config private/server.yaml
```

Only the selected backend's variables are expanded. `LOCAL_LLM_CONFIG` can supply the profile path. Unknown fields, invalid
memory values and non-loopback hosts are rejected. `plan` validates and shows a command without starting it; this output may
contain private local paths. YAML `backend` defaults to `strata`. Change it to `vllm` or `ollama` and run `restart` to switch.
Starting with changed active settings refuses the change until an explicit restart. One state directory owns one backend at a
time; use the same directory and port for alternate profiles. `--backend` is an explicit temporary selection and must also be
supplied to subsequent commands while using that selection.

The shared native port defaults to 54321. Clients use `http://127.0.0.1:<port>/v1/chat/completions`. Readiness checks are specific
to each backend and verify the exact model. An Ollama model can be installed without being GPU-loaded: its first inference
loads it. A ready server alone does not establish inference or persistence. No LAN listener, firewall rule, forwarding or
tunnel is installed.

## Backend settings

Strata needs its own native installation and Python environment with `serve.server`. Set `strata.home` and `strata.config` to
that installation and its private JSON. YAML `options` override resident expert RAM, VRAM reserve, context, resident KV,
prefill, PLE I/O and CPU sharing. The original JSON is preserved; effective JSON is generated in the private state directory.
The example uses 8 GiB resident experts, direct PLE reads, no CPU sharing and a 10,000 MiB VRAM reserve. Resident experts are one
cache budget, not total process RAM. A VRAM reserve does not control GPU compute utilization.

vLLM Docker mode requires an existing local image and a self-contained local model directory, mounted read-only. Host port
publishing is loopback-only. Container RAM equals `memory.ram_gib`; equal memory-plus-swap capacity prevents container swap.
Images are never pulled; model fetching and usage statistics are disabled. Native mode requires Linux. YAML controls GPU
memory utilization (0.9 by default), context, sequences and CPU offload. Concurrency defaults to one and CPU offload to zero.
The optional `swap_space_gib` passes the legacy `--swap-space` flag only when explicitly configured; current vLLM versions no
longer expose that flag, so the default profile omits it. Container OS swap remains disabled independently. See
[vLLM engine arguments](https://docs.vllm.ai/en/latest/configuration/engine_args/).

Ollama runs `ollama serve` with a loopback host, one model, one parallel inference and cloud features disabled. YAML controls
the installed model, model directory, context and GPU overhead reserve. This reserve is a scheduling hint, not a hard VRAM cap;
Ollama can offload to RAM when a model does not fit. No model pull is issued. See [Ollama configuration](https://docs.ollama.com/faq)
and [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility).

## Memory and lifetime

YAML `memory` controls `ram_gib`, `available_gib`, `startup_available_gib`, `free_commit_gib` and `job_commit_gib`. The Windows
supervisor starts the selected process suspended, assigns a kill-on-close job, and resumes it. With the example settings,
children inherit a 100 GiB aggregate **commit** limit, including Windows GPU backing. Commit is not physical RAM. Startup
requires 64 GiB available physical RAM and 148 GiB free host commit. Every 0.5 seconds the supervisor stops the tree above
32 GiB physical RAM, below 32 GiB host-available RAM or below 48 GiB free host commit. This is a sampled cutoff, not an
OS-enforced working-set cap. Defaults target a large-memory workstation and may refuse smaller machines. Linux uses
process-tree and host physical-RAM monitoring; Windows job commit enforcement is not claimed there.

Docker workloads are outside the Windows job: process-tree RSS describes the Docker client, not the VM container. The separate
container cap and host-available RAM guard apply. Container cleanup verifies an instance ownership label. PID ownership checks
creation time and the runner specification path. The root runner detaches from the invoking execution job. Temporary snapshot
reader locks cannot kill healthy inference. Protection stops are recorded in `llm-memory.json`; nothing automatically restarts
or falls back. RAM and GPU are shared with other applications, so these controls do not replace workload validation.

State, effective configuration, PID/lock records, logs and benchmark outputs stay outside Git. The environment-only Strata
launch with `STRATA_HOME` and `STRATA_CONFIG` remains available when no YAML profile is selected.

## Performance comparison

Strata is the default operational choice, **not a claim of measured superiority**. Compare identical source weights,
quantization, context, sampling, prompts, output budgets, concurrency, cache/warm-up state and hardware. Different models or
quantization measure different workloads and do not establish which server is faster or better.

```powershell
python -m ares.benchmark --config private/server.yaml --output private/strata.csv
```

The benchmark measures only the exact selected, already-running backend and model. It never starts or switches engines. Three
small instruction, arithmetic and extraction cases record correctness, wall latency, completion tokens and end-to-end output
tokens per second when supplied. Missing usage stays missing. The rate includes prompt processing, not just decoding. Repeated
requests can reuse caches; record cold and warm runs separately. These smoke cases do not establish analyst-report quality,
DB persistence, long-context capacity or sustained throughput. Record hardware, engine version and model/quantization
provenance alongside any comparison. The CSV saves prompt hashes, not raw prompts or responses. Memory columns report
host-available physical RAM after each request and the runner's process-tree peak since engine startup; Docker client RSS is
not container memory.

## Development and distribution

```powershell
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check src tests
python -m ruff format --check src tests
```

Tests use tiny processes and mocked services without a GPU, model, database or credentials. Native Windows job tests skip on
other operating systems. Engine and model licensing remain separate from this launcher.

Only generic source, tests, metadata, documentation and examples belong in Git. The allowlist ignore file excludes private YAML,
`.env`, weights, logs, environments and build products by default. Review staged files and the complete history before future
publication; ignoring a file cannot sanitize an already tracked secret. No external remote is configured.
