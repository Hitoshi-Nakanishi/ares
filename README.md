# Ares

[![Tests](https://github.com/Hitoshi-Nakanishi/ares/actions/workflows/tests.yml/badge.svg)](
https://github.com/Hitoshi-Nakanishi/ares/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20Linux-lightgrey)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**One YAML file to start, stop and monitor your local LLM server.**

Ares manages **vLLM** (default), **SGLang**, **Ollama** and **Strata** on a GPU workstation. It checks available memory before
starting and monitors the server while it runs, so inference can share the machine with your other applications.

- **One set of commands** for all four engines.
- **Memory protection** for the server process and the host, plus Docker RAM limits.
- **Predictable operation:** one selected engine, explicit restarts, no automatic fallback or model downloads.
- **Local API:** your apps connect directly to the engine on `127.0.0.1`.

## Quick start

You need Python 3.11+, a virtual environment, and an installed engine and model. The default example uses vLLM in Docker with
GPU access. See the [engine requirements](GUIDE.md#engines) for other setups.

From the Ares checkout:

```sh
python -m pip install -e .
```

Copy [the example profile](examples/server.example.yaml) to `private/server.yaml`, creating the `private` directory first.
Replace `VLLM_IMAGE`, `VLLM_MODEL_PATH` and `VLLM_MODEL` placeholders with your installed image, local model directory and API
model name, or set those environment variables. Keep private profiles outside Git.

```sh
python -m ares plan --config private/server.yaml
python -m ares start --config private/server.yaml
python -m ares status --config private/server.yaml
```

`plan` shows the launch command without starting anything. Once ready, point your OpenAI-compatible client at
`http://127.0.0.1:54321/v1` and use the model name from your profile.

## Everyday commands

Use `--config <profile>` with each command, or set `LOCAL_LLM_CONFIG` once.

| Command | What it does |
| --- | --- |
| `start` | Start the server; reuse it if the same profile is already ready |
| `status` | Check the managed process, engine readiness and model |
| `stop` | Stop the server and its children |
| `restart` | Apply configuration changes by stopping and starting |
| `plan` | Validate the profile and preview the launch command |
| `autostart` | Start if memory permits; otherwise skip without retrying |
| `recover` | Retry eligible failures under an explicitly enabled, bounded recovery policy |

After `stop` or a protection trip, inspect the reason, then explicitly resume. An opt-in [recovery policy](GUIDE.md#automatic-recovery)
can retry driver faults and stalled or failed engines; operator and memory stops remain paused.

```sh
python -m ares start --resume --config private/server.yaml
```

Changing engines means changing `backend` in the profile and restarting. Configure the new engine first; a failed replacement
leaves the server stopped. See [profiles and switching](GUIDE.md#profiles-and-switching).

## Memory and lifetime

Review the profile's memory limits before starting: the example targets a large-memory workstation. A protection stop pauses
inference until you resume it; Ares does not restart the server, Docker or the host automatically.

[Memory settings and recovery](GUIDE.md#memory-and-recovery) explains host reserves, Docker limits and GPU stall detection.

## More

- [Configuration and operations](GUIDE.md): engine options, application startup and benchmarks.
- [Contributing](CONTRIBUTING.md): development setup and checks.
- [Security](SECURITY.md): trust model and private vulnerability reports.

[MIT License](LICENSE). Engines, models and dependencies keep their own licenses; see
[third-party notices](THIRD_PARTY_NOTICES.md).
