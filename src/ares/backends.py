"""Build bounded backend commands without a shell or model downloads."""

import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath

from ares.config import checked


def number(value, name, minimum=0, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def docker_source(value, name):
    # An absolute source is a directory of the Docker host VM, such as a WSL distro's ext4 disk.
    if value.startswith("/") and ".." not in PurePosixPath(value).parts and re.fullmatch(r"[A-Za-z0-9_./-]+", value):
        return f"type=bind,source={value}"
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        return f"type=volume,source={value}"
    raise ValueError(f"{name} must be a Docker volume name or an absolute directory of the Docker host VM")


def model_mount(settings, backend):
    """Return the read-only model mount and the model path inside the container."""
    if volume := settings.get("volume"):
        inside = PurePosixPath(settings["model_path"])
        if inside.is_absolute() or not inside.parts or ".." in inside.parts:
            raise ValueError(f"{backend}.model_path must be a relative directory inside the volume")
        return f"{docker_source(volume, f'{backend}.volume')},target=/models,readonly", f"/models/{inside}"
    model_path = Path(settings["model_path"]).resolve()
    if not model_path.is_dir():
        raise ValueError(f"{backend}.model_path must be an existing local model directory")
    return f"type=bind,source={model_path},target=/model,readonly", "/model"


def container_environment(settings, backend, managed):
    """Validate the container variables, image command and engine arguments a profile adds."""
    extra_env = settings.get("env", {})
    if not isinstance(extra_env, dict) or not all(
        re.fullmatch(r"[A-Z_][A-Z0-9_]*", str(name))
        and isinstance(value, (str, int, float))
        and not isinstance(value, bool)
        for name, value in extra_env.items()
    ):
        raise ValueError(f"{backend}.env must map upper-case variable names to values")
    for name in ("args", "image_command"):
        items = settings.get(name, [])
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise ValueError(f"{backend}.{name} must be a list of strings")
    if managed.intersection(item.split("=", 1)[0] for item in settings.get("args", [])):
        raise ValueError(f"{backend}.args cannot repeat flags that Ares sets from YAML fields")
    return {name: str(value) for name, value in extra_env.items()}


def docker_run(spec, settings, mounts, container_port, options=(), *, materialize):
    """Start an installed image with the GPU, a RAM cap without swap and a loopback-only port."""
    backend = spec["backend"]
    image = settings["image"]
    if not image or image.startswith("-"):
        raise ValueError(f"Set an installed {backend}.image")
    security = []
    if settings.get("seccomp_profile"):
        seccomp = Path(settings["seccomp_profile"]).resolve()
        if not seccomp.is_file():
            raise ValueError(f"{backend}.seccomp_profile must be an existing JSON file")
        security = ["--security-opt", f"seccomp={seccomp}"]
    limit = spec["memory"]["ram_gib"]
    spec["docker_name"] = f"ares-{backend}"
    spec["docker_owner"] = uuid.uuid4().hex if materialize else "plan"
    return [
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
        f"--shm-size={number(settings.get('shm_size_gib', 1), 'shm_size_gib', 1, 64)}g",
        *options,
        *security,
        "-p",
        f"127.0.0.1:{spec['port']}:{container_port}",
        *(item for mount in mounts for item in ("--mount", mount)),
        *(item for name, value in spec["env"].items() for item in ("-e", f"{name}={value}")),
        image,
        *settings.get("image_command", []),
    ]


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
            (
                "resident_budget_gib",
                "vram_reserve_mib",
                "max_context",
                "kv_resident",
                "prefill",
                "expert_cache",
                "ple_io",
                "cpu_share",
            ),
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
            elif key in ("prefill", "expert_cache") and value == "auto":
                pass
            else:
                number(value, key)
                if key == "expert_cache" and not isinstance(value, int):
                    raise ValueError("Invalid expert_cache")
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
                "image_command",
                "volume",
                "model_path",
                "model",
                "gpu_memory_utilization",
                "max_model_len",
                "max_num_seqs",
                "cpu_offload_gb",
                "swap_space_gib",
                "shm_size_gib",
                "seccomp_profile",
                "env",
                "args",
            ),
            "vllm",
        )
        mode = settings.get("mode", "docker")
        if settings.get("volume") and mode != "docker":
            raise ValueError("vllm.volume needs docker mode")
        mount, container_model = model_mount(settings, "vllm")
        environment = container_environment(
            settings,
            "vllm",
            {
                "--model",
                "--host",
                "--port",
                "--served-model-name",
                "--gpu-memory-utilization",
                "--max-model-len",
                "--max-num-seqs",
                "--cpu-offload-gb",
                "--swap-space",
            },
        )
        utilization = number(settings.get("gpu_memory_utilization", 0.9), "gpu_memory_utilization", 0.01, 0.98)
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
        arguments.extend(settings.get("args", []))
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", VLLM_NO_USAGE_STATS="1")
        spec.update(model=settings["model"], env=environment)
        if mode == "docker":
            spec["command"] = [
                *docker_run(spec, settings, [mount], 8000, materialize=materialize),
                "--model",
                container_model,
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
                str(Path(settings["model_path"]).resolve()),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *arguments,
            ]
        else:
            raise ValueError("vllm.mode must be docker or native")
    elif backend == "sglang":
        checked(
            settings,
            (
                "image",
                "image_command",
                "volume",
                "model_path",
                "model",
                "model_env",
                "container_port",
                "cache",
                "shm_size_gib",
                "seccomp_profile",
                "env",
                "args",
            ),
            "sglang",
        )
        mount, container_model = model_mount(settings, "sglang")
        mounts = [mount]
        if settings.get("cache"):
            mounts.append(f"{docker_source(settings['cache'], 'sglang.cache')},target=/cache")
        environment = container_environment(
            settings, "sglang", {"--model-path", "--host", "--port", "--served-model-name"}
        )
        container_port = settings.get("container_port", 30000)
        if isinstance(container_port, bool) or not isinstance(container_port, int) or not 1 <= container_port <= 65535:
            raise ValueError("sglang.container_port must be an integer between 1 and 65535")
        if model_env := settings.get("model_env"):
            # A recipe image reads the model path from a variable and refuses server arguments.
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", model_env) or model_env in environment:
                raise ValueError("sglang.model_env must be an upper-case variable name that env does not set")
            environment[model_env] = container_model
            arguments = settings.get("args", [])
        else:
            arguments = [
                "--model-path",
                container_model,
                "--host",
                "0.0.0.0",
                "--port",
                str(container_port),
                "--served-model-name",
                settings["model"],
                *settings.get("args", []),
            ]
        environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        spec.update(model=settings["model"], env=environment)
        # Host-RAM caches pin memory, which needs an unlimited locked-memory limit.
        options = ["--init", "--ulimit", "memlock=-1:-1"]
        spec["command"] = [
            *docker_run(spec, settings, mounts, container_port, options, materialize=materialize),
            *arguments,
        ]
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
