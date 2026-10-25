# Ares

Ares manages a workstation's GPU inference servers.

Local-only launch and memory supervision for **Strata (default)**, **vLLM**, and **Ollama**. This is server management, not an
agent framework or inference engine. Model weights, credentials, application workflows and databases are excluded. There is no
automatic fallback, model download, external listener or boot task. Strata and Ollama run natively on Windows; vLLM uses an
already installed GPU Docker image or a native Linux installation.

## Install and run

Use Python 3.11 or newer. Install the selected engine and model separately. Copy `examples/server.example.yaml` to an ignored or
external private location. Set its paths and model names, or supply its `${VARIABLE}` values through the environment.

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e .
```

Run these commands from the Ares checkout in PowerShell. Set `$config` to your private YAML file; its path need not be inside
this repository. On Linux, use `.venv/bin/python` instead.

```powershell
$python = ".\.venv\Scripts\python.exe"
$config = "private/server.yaml"

# Start, or return successfully if the same configuration is already ready.
& $python -m ares start --config $config

# Check the managed process, backend readiness and exact model.
& $python -m ares status --config $config

# Stop the managed server and its children.
& $python -m ares stop --config $config

# Stop and start again to apply configuration changes.
& $python -m ares restart --config $config

# Explicitly resume after an operator stop or a safety trip.
& $python -m ares start --resume --config $config

# Validate and inspect the launch command without starting or stopping anything.
& $python -m ares plan --config $config
```

Only the selected backend's variables are expanded. `LOCAL_LLM_CONFIG` can supply the profile path. Unknown fields, invalid
memory values and non-loopback hosts are rejected. `plan` validates and shows a command without starting it; this output may
contain private local paths. Starting with changed active settings refuses the change until an explicit restart. One state
directory owns one backend at a time; use the same directory and port for alternate profiles. Before stopping or restarting, let
application requests finish and pause any application automation that could start the server again.

Another project can invoke the same interpreter and module with absolute paths and `--config`, or export `LOCAL_LLM_CONFIG` to
its child process. Its application startup can call `start` repeatedly with the same profile without creating duplicate servers.
Ares itself does not install a Windows boot task or restart after a memory-protection stop.

`stop`, a supervised failure, or an unexpected exit of a recorded runner inhibits subsequent starts using
`llm-paused.json` in the selected state directory. Ordinary `start` and `restart` refuse; `autostart` skips. Inspect the reason
and host health before explicitly using `start --resume` or `restart --resume`. `autostart` rejects `--resume`.
The startup memory checks still apply, and an insufficient reserve leaves the pause intact. Resuming does not authorize an
application to resume its report queue or periodic maintenance. The Windows job enforces below-normal CPU priority throughout
the native process tree in addition to its aggregate committed-memory limit; RSS remains a separately sampled cutoff.

For host-application startup, use `autostart` after the application's other services are ready:

```powershell
& $python -m ares autostart --config $config
```

It uses the same profile and safeguards as `start`. If startup RAM or free commit is insufficient, or memory headroom cannot be
established, it prints `Ares autostart skipped` and exits successfully without launching inference. Without a YAML profile it
also skips. An existing ready instance is preserved. It never reduces the configured reserves or retries in a loop;
configuration and ownership errors still fail. The host application owns any login trigger or later retry. With the example
profile, a new launch requires 64 GiB available physical RAM and 148 GiB free host commit. After a skip, run `autostart` again
when other workloads have finished.

The shared native port defaults to 54321. Clients use `http://127.0.0.1:<port>/v1/chat/completions`. Readiness checks are
specific to each backend and verify the exact model. An Ollama model can be installed without being GPU-loaded: its first
inference loads it. A ready server alone does not establish inference or persistence. No LAN listener, firewall rule, forwarding
or tunnel is installed.

## Backend settings

Edit the top-level `backend` field in the private YAML. Choose exactly one of these values, then run `restart`:

```yaml
backend: strata
```

| Value | Required backend configuration |
| --- | --- |
| `strata` | `backends.strata.home` and `config`, pointing to the installed native engine and its JSON model configuration |
| `vllm` | `backends.vllm.image`, `model_path` and `model`, identifying a local Docker image, model directory and API model name |
| `ollama` | `backends.ollama.executable` and `model`, identifying an installed executable and an already installed model |

For example, replace `backend: strata` with `backend: ollama` and run:

```powershell
& $python -m ares plan --config $config
& $python -m ares restart --config $config
& $python -m ares status --config $config
```

The selected backend must be fully configured before restarting. The complete layout is in
[`examples/server.example.yaml`](examples/server.example.yaml). Replace its `${VARIABLE}` placeholders with local values or set
those environment variables. Switching stops the previous engine first; a failed replacement leaves it stopped, without
fallback. `--backend` can override the YAML selection for a command, but must also be supplied to subsequent commands while
using that selection. Server switching does not automatically adapt an application's model name, API extensions or output
validation; verify those contracts before sending application workloads to another backend.

Strata needs its own native installation and Python environment with `serve.server`. Set `strata.home` and `strata.config` to
that installation and its private JSON. YAML `options` override resident expert RAM, VRAM reserve, context, resident KV,
prefill, PLE I/O and CPU sharing. The original JSON is preserved; effective JSON is generated in the private state directory.
The example uses 8 GiB resident experts, direct PLE reads, no CPU sharing and a 10,000 MiB VRAM reserve. Resident experts are
one cache budget, not total process RAM. A VRAM reserve does not control GPU compute utilization.

vLLM Docker mode requires an existing local image and a self-contained local model directory, mounted read-only. Host port
publishing is loopback-only. Container RAM equals `memory.ram_gib`; equal memory-plus-swap capacity prevents container swap.
Images are never pulled; model fetching and usage statistics are disabled. Native mode requires Linux. YAML controls GPU memory
utilization (0.9 by default), context, sequences and CPU offload. Concurrency defaults to one and CPU offload to zero. The
optional `swap_space_gib` passes the legacy `--swap-space` flag only when explicitly configured; current vLLM versions no longer
expose that flag, so the default profile omits it. Container OS swap remains disabled independently. A model kept in a Docker
named volume uses `volume` with `model_path` relative to it; the volume is mounted read-only at `/models`, so weights stay on
the Linux disk instead of crossing the Windows file share. An absolute `volume` is a directory of the Docker host VM, bound
the same way; Docker Desktop reaches a directory that a WSL distro bind-mounts under `/mnt/wsl` at `/run/desktop/mnt/host/wsl`. `image_command` goes between the image and the engine arguments
(for example `[vllm, serve]` for an image whose entrypoint is a launcher), `env` adds container variables, `args` appends engine
arguments that no YAML field sets, `shm_size_gib` sizes `/dev/shm` (1 GiB by default) and utilization may reach 0.98.
Docker's default seccomp profile refuses io_uring, which engines that read weights from disk during inference need;
`seccomp_profile` names a replacement. [`examples/seccomp-io-uring.json`](examples/seccomp-io-uring.json) keeps the syscalls
Docker documents as blocked by default (mounts, namespaces, tracing, key and kernel-module calls) and allows io_uring. See
[vLLM engine arguments](https://docs.vllm.ai/en/latest/configuration/engine_args/).

Ollama runs `ollama serve` with a loopback host, one model, one parallel inference and cloud features disabled. YAML controls
the installed model, model directory, context and GPU overhead reserve. This reserve is a scheduling hint, not a hard VRAM cap;
Ollama can offload to RAM when a model does not fit. No model pull is issued. See
[Ollama configuration](https://docs.ollama.com/faq) for server settings. The
[OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility) reference describes API support.

## Memory and lifetime

Set `memory.enforce_runtime: false` to give an admitted native service continuous availability without automatic memory
stops. Startup still checks the configured host RAM and commit headroom. The detached runner then waits for the server;
it does not poll memory, install a Windows job commit limit, or latch an unexpected exit as a safety pause. Explicit
operator stops still require `--resume`. The application owns recovery and must budget lower-priority workloads separately.
The remaining memory fields describe startup admission when runtime enforcement is disabled, not reserved or enforced RAM.
Docker backend container limits are independent and still apply.

Edit the existing top-level `memory` block in the private YAML, then run `plan` and `restart` to apply it. Values are in GiB (1
GiB = 1,073,741,824 bytes). These are limits and headroom checks, not amounts preallocated at startup.

```yaml
memory:
  ram_gib: 32
  available_gib: 32
  startup_available_gib: 64
  free_commit_gib: 48
  job_commit_gib: 100
```

| Field | Meaning |
| --- | --- |
| `ram_gib` | Stop native inference when aggregate process-tree physical RAM exceeds this value; also the Docker container memory cap |
| `available_gib` | Stop when the host's available physical RAM falls below this value, preserving room for other applications |
| `startup_available_gib` | Require at least this much available host physical RAM before starting |
| `free_commit_gib` | Stop when free host commit falls below this value |
| `job_commit_gib` | Windows job aggregate commit limit, including GPU backing; separate from physical RAM |

To allow up to 48 GiB of native process-tree RAM, change only `ram_gib: 32` to `ram_gib: 48` in that block. Keep the host
headroom fields to preserve room for other applications. `startup_available_gib` must be at least `available_gib`, and
`job_commit_gib` must be at least `ram_gib`. A smaller commit limit can still stop a GPU-heavy model even when its physical RAM
use is low.

Strata's cache and VRAM settings are separate from the memory protection limits. Edit these fields in the existing
`backends.strata.options` block:

```yaml
resident_budget_gib: 8
vram_reserve_mib: 10000
```

`resident_budget_gib` budgets the expert cache in RAM, not total application RAM. `vram_reserve_mib` leaves approximately 9.8
GiB of VRAM headroom; it does not set GPU compute utilization. vLLM uses `backends.vllm.gpu_memory_utilization: 0.9` to budget
approximately 90% of VRAM. Ollama uses `backends.ollama.vram_reserve_mib` as a scheduling hint. Model fit and measured usage
still need checking after changes.

Strata `prefill` accepts a numeric chunk size or `auto`. The latter lets the native engine choose a chunk that fits its GPU
cache and context. Small fixed chunks can repeatedly stream uncached experts from disk. Keep memory protection enabled and
verify representative requests and production health after changing this setting; selecting `auto` does not prove throughput.

`expert_cache` accepts an integer expert-blob budget or `auto`. It controls GPU caching separately from
`resident_budget_gib`, which controls RAM caching. On Windows, GPU allocations also consume job commit: calculate that limit
from measured allocation and host free commit before increasing this cache. Keep the physical-RAM cutoff and host reserves
enabled; a larger GPU cache does not authorize consuming that much physical RAM.

The Windows supervisor starts the selected process suspended, assigns a kill-on-close job, and resumes it. With the example
settings, children inherit a 100 GiB aggregate **commit** limit, including Windows GPU backing. Commit is not physical RAM.
Startup requires 64 GiB available physical RAM and 148 GiB free host commit (`job_commit_gib + free_commit_gib`). Every 0.5
seconds the supervisor stops the tree above 32 GiB physical RAM, below 32 GiB host-available RAM or below 48 GiB free host
commit. This is a sampled cutoff, not an OS-enforced working-set cap. Defaults target a large-memory workstation and may refuse
smaller machines. Linux uses process-tree and host physical-RAM monitoring; Windows job commit enforcement is not claimed there.

Docker workloads are outside the Windows job: process-tree RSS describes the Docker client, not the VM container. The separate
container cap and host-available RAM guard apply. Container cleanup verifies an instance ownership label. PID ownership checks
creation time and the runner specification path. The root runner detaches from the invoking execution job. Temporary snapshot
reader locks cannot kill healthy inference. Protection stops are recorded in `llm-memory.json`; nothing automatically restarts
or falls back. RAM and GPU are shared with other applications, so these controls do not replace workload validation.

When a Windows host forbids leaving its job, Ares warns and starts the same runner inside that host job. The configured Ares
memory supervision still applies, but the host can end the runner when it closes its job. Other launch errors remain errors.

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
requests can reuse caches; record cold and warm runs separately. These smoke cases do not establish analyst-report quality, DB
persistence, long-context capacity or sustained throughput. Record hardware, engine version and model/quantization provenance
alongside any comparison. The CSV saves prompt hashes, not raw prompts or responses. Memory columns report host-available
physical RAM after each request and the runner's process-tree peak since engine startup; Docker client RSS is not container
memory.

## Development and distribution

When embedded as a Git submodule, develop in that submodule checkout as the source of truth. Commit Ares changes there first,
then commit the updated submodule pointer in the parent project. A second working copy is unnecessary. Keep the virtual
environment inside this checkout; its paths are local and excluded from Git.

```powershell
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check src tests
python -m ruff format --check src tests
```

Tests use tiny processes and mocked services without a GPU, model, database or credentials. Native Windows job tests skip on
other operating systems. Engine and model licensing remain separate from this launcher.

Only generic source, tests, metadata, documentation and examples belong in Git. The allowlist ignore file excludes private YAML,
`.env`, weights, logs, environments and build products by default. Follow [publication checks](PUBLICATION.md) before publishing;
ignoring a file cannot sanitize an already tracked secret. An existing process whose ownership cannot be verified must be
inspected by its operator before stopping it.

## License and contributions

Ares source is released under the [MIT License](LICENSE). Dependencies, engines and models retain their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md). Use [CONTRIBUTING.md](CONTRIBUTING.md) for development and
[SECURITY.md](SECURITY.md) for confidential vulnerability reporting.
