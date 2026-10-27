# Configuration and operations

[Quick start](README.md#quick-start) · [Example profile](examples/server.example.yaml)

## Profiles and switching

Pass `--config <profile.yaml>` or set `LOCAL_LLM_CONFIG`. The YAML `backend` defaults to `vllm` when omitted. Only the selected
engine's environment variables are expanded; unknown fields, invalid memory values and non-loopback hosts are rejected.

To switch engines, configure its `backends` entry, change `backend`, then run `plan`, `restart` and `status`. Reuse the same
`state_dir` and port: one state directory owns one engine at a time. Let active requests finish and pause application automation
before switching. A failed replacement leaves inference stopped, without fallback.

`--backend` overrides the YAML for one command; repeat it on subsequent commands while using that selection. Clients must use
the selected model name and supported API features. `plan` output can contain private local paths.

## Engines

Install engines, Docker images and model weights separately. Ares uses local installations, mounts model directories read-only
and binds the API to loopback. It never pulls images or downloads models.

| Engine | Runs on | Required fields under `backends.<engine>` |
| --- | --- | --- |
| `vllm` (default) | Docker with GPU access; native Linux | `model_path`, `model`; Docker also needs `image` |
| `sglang` | Docker with GPU access | `image`, `model_path`, `model` |
| `ollama` | Native Windows or Linux | `executable`, `model` |
| `strata` | Native installation | `home`, `config` |

### vLLM

`mode: docker` uses an installed image; `mode: native` uses Linux. Set `gpu_memory_utilization`, `max_model_len`, `max_num_seqs`
and `cpu_offload_gb` to suit your model. The example starts with one concurrent request and no CPU offload. `swap_space_gib` is
only for older engines supporting `--swap-space`; omit it for current versions. Container OS swap is always disabled.

### Docker options for vLLM and SGLang

| Field | Use |
| --- | --- |
| `model_path` | Existing local model directory, mounted read-only at `/model` |
| `volume` | Docker named volume or absolute Docker-host directory; `model_path` becomes relative to it, under `/models` |
| `image_command` | Command between the image name and engine arguments, such as `[vllm, serve]` |
| `env` | Additional container environment variables |
| `args` | Engine arguments not already set by YAML fields |
| `shm_size_gib` | Shared memory size; defaults to 1 GiB |
| `seccomp_profile` | Optional seccomp JSON for engines needing io_uring |

Docker Desktop can bind a WSL directory shared under `/mnt/wsl` through `/run/desktop/mnt/host/wsl`. This keeps weights on the
Linux disk rather than the Windows file share. The [io_uring profile](examples/seccomp-io-uring.json) allows those calls while
retaining the blocks on mounts, namespaces, tracing, keys and kernel modules.

SGLang also supports `container_port` (default 30000) and a writable `cache` mounted at `/cache` for compiled kernels. Use
`image_command: [python3, -m, sglang.launch_server]` for a server image. Recipe images that reject arguments can use `model_env`
to receive the model path; set their other options in `env`. The configured port and model must match the recipe. SGLang
containers get an init process and unlimited locked memory for pinned caches.

Engine references: [vLLM arguments](https://docs.vllm.ai/en/latest/configuration/engine_args/),
[SGLang arguments](https://docs.sglang.ai/advanced_features/server_arguments.html).

### Ollama

Ares runs `ollama serve` with one model, one parallel request and cloud features disabled. Set `context_length` and, optionally,
`models_dir` and `vram_reserve_mib`. The VRAM reserve is a scheduling hint; Ollama can offload to RAM. An installed model loads
onto the GPU on its first inference request.

See [Ollama configuration](https://docs.ollama.com/faq) and
[API compatibility](https://docs.ollama.com/api/openai-compatibility).

### Strata

`home` points to the native installation with its own virtual environment and `serve.server`; `config` points to its model JSON.
YAML `options` generate an effective JSON in `state_dir`, leaving the original unchanged.

- `resident_budget_gib` controls the expert cache in RAM; it is not total process RAM.
- `vram_reserve_mib` leaves GPU memory headroom; it does not limit compute utilization.
- `prefill` accepts a chunk size or `auto`; small chunks can repeatedly read experts from disk.
- `expert_cache` accepts an integer budget or `auto` for GPU caching. GPU allocations also consume Windows commit.
- Context, resident KV, PLE I/O and CPU sharing options are shown in the example profile.

The legacy environment-only entry point uses `STRATA_HOME` and `STRATA_CONFIG` when no YAML profile is selected. Use a YAML
profile for vLLM.

## Memory and recovery

Set the top-level `memory` fields, then run `plan` and `restart`. Values are GiB and describe limits or required free capacity,
not preallocated memory.

| Field | Default | Meaning |
| --- | ---: | --- |
| `ram_gib` | 32 | Native process-tree RAM cutoff; also the Docker container RAM cap |
| `available_gib` | 32 | Minimum available host RAM during inference |
| `startup_available_gib` | 64 | Minimum available host RAM before launch |
| `free_commit_gib` | 48 | Minimum free Windows commit during inference |
| `job_commit_gib` | 100 | Windows native process-tree commit limit, including GPU backing |

Startup requires `startup_available_gib` of available RAM and, on Windows, `job_commit_gib + free_commit_gib` of free commit.
These defaults target a large-memory workstation. Keep `startup_available_gib >= available_gib` and `job_commit_gib >= ram_gib`;
GPU-heavy processes may need much more commit than physical RAM.

Native process-tree RAM and host headroom are sampled every 0.5 seconds. On Windows, a job also limits aggregate commit and sets
below-normal CPU priority for the process tree. Linux monitors physical RAM; it does not enforce Windows commit limits. The
Docker VM is outside the Windows job, so container RAM/no-swap limits apply separately. Docker client RSS does not measure
container memory. Shared host resources still need budgeting for other applications.

`memory.enforce_runtime: false` disables runtime memory polling, the Windows job commit limit and the unexpected-exit pause.
Startup checks, Docker limits, GPU fault detection and explicit operator-stop pauses still apply. Applications using this mode
own recovery and must budget other workloads.

### Stops and resume

`stop`, a protection trip or an unexpected recorded runner exit writes `llm-paused.json` in `state_dir`. Ordinary `start` and
`restart` refuse while paused; `autostart` skips. Inspect the reason and host health, then use `start --resume` or
`restart --resume`. Failed startup preserves the pause. `autostart` cannot resume.

The runner checks GPU faults every 15 seconds. On Windows, new NVIDIA events 13, 14 or 153, or Display event 4101, stop the
server. For vLLM and SGLang, running requests without engine-step or token progress for 300 seconds, or an unresponsive server
for that long after readiness, also trigger a stop. SGLang needs `--enable-metrics`. Unreadable logs or metrics alone do not
stop inference. Ares never restarts Docker, WSL or Windows after a fault.

Logs, PID records and `llm-memory.json` stay in the private state directory. Ownership is verified before stopping a process or
container; inspect an unverifiable process manually. If Windows prevents the runner from leaving the caller's job, Ares warns
and runs inside it: closing that parent job can end inference.

## Application startup

### Automatic recovery

For a vLLM or SGLang profile, enable recovery explicitly in the private YAML:

```yaml
recovery:
  enabled: true
  cooldown_seconds: 300
  window_seconds: 3600
  max_attempts: 3
```

A host scheduler can call `python -m ares recover --config private/server.yaml --wait 5`. This action resumes only recorded
driver faults, stalled engines or failed processes. It checks that `nvidia-smi` responds, waits after a failure and between
attempts, and records the attempt before starting the same model. The attempt history survives scheduler and process restarts.
Startup memory reserves and process/container ownership checks still apply. An operator stop, memory protection stop,
unreadable state or exhausted attempt budget is left paused. Ordinary `autostart` continues to respect every pause.

Recovery restarts the managed inference server. It does not reboot the host, reset a display adapter, alter driver settings,
restart Docker or select another backend. A GPU that remains unresponsive requires operator diagnosis. Ares installs no
recurring task itself; the host application owns its scheduler and any retries of application work.

### Guarded startup

Call `autostart` after your application's other services are ready. It preserves a ready server and skips successfully when
memory headroom is insufficient or unreadable, or no YAML profile is selected. Configuration and ownership errors still fail. It
does not lower reserves or retry; your application owns login triggers, scheduling and any later retry.

Repeated `start` calls with the same ready profile do not create duplicate servers. Clients connect directly to
`http://127.0.0.1:<port>/v1/chat/completions`. Readiness verifies the engine and exact model; check real inference separately.
Ares installs no boot task, LAN listener or authentication layer. See the [security model](SECURITY.md).

## Benchmarks

Measure an already-running profile:

```sh
python -m ares.benchmark --config private/server.yaml --output private/benchmark.csv
```

Three arithmetic, instruction and extraction cases record correctness, elapsed time, completion tokens and end-to-end tokens per
second where available. CSVs store prompt hashes, not raw prompts or replies. Memory columns show host-available RAM and the
runner's native process-tree peak since startup, not container memory.

Compare the same weights, quantization, context, sampling, prompts, output budget, concurrency and hardware. Record cold and
warm runs separately. These smoke cases measure neither application quality nor sustained throughput; choosing a default engine
does not establish a performance ranking.
