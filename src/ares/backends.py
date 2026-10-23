"""Build bounded backend commands without a shell or model downloads."""

import json
import os
import uuid
from pathlib import Path

from ares.config import checked


def number(value, name, minimum=0, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def build_spec(profile, *, materialize=False):
    backend, port, settings = profile["backend"], profile["port"], profile["settings"]
    state = Path(profile["state_dir"])
    spec = {k: v for k, v in profile.items() if k != "settings"}
    spec.update(cwd=str(state), env={}, cleanup=[])
    if backend == "strata":
        checked(settings, ("home", "config", "options"), "strata")
        home = Path(settings["home"]).resolve()
        native_path = Path(settings["config"]).resolve()
        native = json.loads(native_path.read_text(encoding="utf-8-sig"))
        options = checked(
            settings.get("options", {}),
            ("resident_budget_gib", "vram_reserve_mib", "max_context", "kv_resident", "prefill", "ple_io", "cpu_share"),
            "strata.options",
        )
        arguments = list(native["args"])
        for key, value in options.items():
            if key == "cpu_share":
                number(value, key, 0, 1)
                native.setdefault("env", {})["STRATA_PREFILL_CPU_SHARE"] = str(value)
                continue
            if key == "ple_io":
                if value not in ("direct", "mmap"):
                    raise ValueError("ple_io must be direct or mmap")
            elif key == "prefill" and value == "auto":
                pass
            else:
                number(value, key)
            flag = "--" + key.replace("_", "-")
            if flag in arguments:
                index = arguments.index(flag)
                arguments[index + 1] = str(value)
            else:
                arguments.extend((flag, str(value)))
        native.update(args=arguments, host="127.0.0.1", port=port, open_browser=False)
        effective = state / "strata-effective.json"
        if materialize:
            state.mkdir(parents=True, exist_ok=True)
            effective.write_text(json.dumps(native, indent=2), encoding="utf-8")
        python = home / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        spec.update(
            model=native["model_name"],
            cwd=str(home),
            command=[
                str(python),
                "-u",
                "-m",
                "serve.server",
                "--engine",
                "strata",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--config",
                str(effective),
            ],
        )
    elif backend == "vllm":
        checked(
            settings,
            (
                "mode",
                "executable",
                "image",
                "model_path",
                "model",
                "gpu_memory_utilization",
                "max_model_len",
                "max_num_seqs",
                "cpu_offload_gb",
                "swap_space_gib",
            ),
            "vllm",
        )
        model_path = Path(settings["model_path"]).resolve()
        if not model_path.is_dir():
            raise ValueError("vllm.model_path must be an existing local model directory")
        utilization = number(settings.get("gpu_memory_utilization", 0.9), "gpu_memory_utilization", 0.01, 0.95)
        arguments = [
            "--served-model-name",
            settings["model"],
            "--gpu-memory-utilization",
            str(utilization),
            "--max-model-len",
            str(number(settings.get("max_model_len", 8192), "max_model_len", 1)),
            "--max-num-seqs",
            str(number(settings.get("max_num_seqs", 1), "max_num_seqs", 1)),
            "--cpu-offload-gb",
            str(number(settings.get("cpu_offload_gb", 0), "cpu_offload_gb")),
        ]
        if "swap_space_gib" in settings:
            arguments.extend(("--swap-space", str(number(settings["swap_space_gib"], "swap_space_gib"))))
        spec.update(
            model=settings["model"],
            env={"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "VLLM_NO_USAGE_STATS": "1"},
        )
        mode = settings.get("mode", "docker")
        if mode == "docker":
            image = settings["image"]
            if not image or image.startswith("-"):
                raise ValueError("Set an installed vllm.image")
            limit = profile["memory"]["ram_gib"]
            spec["docker_name"] = "ares-vllm"
            spec["docker_owner"] = uuid.uuid4().hex if materialize else "plan"
            spec["command"] = [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--name",
                spec["docker_name"],
                "--label",
                "ares.managed=true",
                "--label",
                f"ares.owner={spec['docker_owner']}",
                "--gpus",
                "all",
                f"--memory={limit}g",
                f"--memory-swap={limit}g",
                "--shm-size=1g",
                "-p",
                f"127.0.0.1:{port}:8000",
                "--mount",
                f"type=bind,source={model_path},target=/model,readonly",
                "-e",
                "HF_HUB_OFFLINE=1",
                "-e",
                "TRANSFORMERS_OFFLINE=1",
                "-e",
                "VLLM_NO_USAGE_STATS=1",
                image,
                "--model",
                "/model",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                *arguments,
            ]
        elif mode == "native":
            if os.name == "nt":
                raise ValueError("Native vLLM requires Linux; select docker on Windows")
            spec["command"] = [
                settings.get("executable", "vllm"),
                "serve",
                str(model_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *arguments,
            ]
        else:
            raise ValueError("vllm.mode must be docker or native")
    else:
        checked(settings, ("executable", "model", "models_dir", "context_length", "vram_reserve_mib"), "ollama")
        spec.update(
            model=settings["model"],
            command=[settings.get("executable", "ollama"), "serve"],
            env={
                "OLLAMA_HOST": f"127.0.0.1:{port}",
                "OLLAMA_NUM_PARALLEL": "1",
                "OLLAMA_MAX_LOADED_MODELS": "1",
                "OLLAMA_NO_CLOUD": "1",
                "OLLAMA_CONTEXT_LENGTH": str(number(settings.get("context_length", 8192), "context_length", 1)),
                "OLLAMA_GPU_OVERHEAD": str(
                    int(number(settings.get("vram_reserve_mib", 10000), "vram_reserve_mib") * 2**20)
                ),
            },
        )
        if settings.get("models_dir"):
            spec["env"]["OLLAMA_MODELS"] = str(Path(settings["models_dir"]).resolve())
    if not isinstance(spec["model"], str) or not spec["model"] or spec["model"].startswith("-"):
        raise ValueError("A valid local model name is required")
    if materialize:
        state.mkdir(parents=True, exist_ok=True)
    return spec
