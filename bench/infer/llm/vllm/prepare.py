"""Start the isolated vLLM test container and prepare its adapter."""

import json
import os
import shlex
import subprocess
from pathlib import Path

from bench.infer.llm.vllm.compat.compat import prepare_runtime


def shell_value(value: object) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def main() -> None:
    context = json.loads(Path(os.environ["INFRAPERF_CONTEXT"]).read_text())
    root = Path(context["root"])
    temporary = Path(context["runtime"])
    runtime = temporary / "source"
    prepare_runtime(runtime)
    settings = {"SPE_ENABLE": 0, "GROUP_SET_920B": "extended", **context["settings"]}
    container = f"infraperf-{context['run_id'].lower()}"
    settings.update(PROJECT=str(runtime), CONTAINER=container, GPU_ID=0)
    (runtime / "baseline.env").write_text(
        "".join(f"{name}={shlex.quote(shell_value(value))}\n"
                for name, value in settings.items() if name not in {"IMAGE", "GPU_DEVICE"}),
        encoding="utf-8",
    )
    model = Path(settings["MODEL"]).resolve()
    subprocess.run(
        ["docker", "run", "--rm", "-d", "--name", container, "--network", "host",
         "--gpus", f"device={settings.get('GPU_DEVICE', 0)}", "--privileged",
         "--security-opt", "seccomp=unconfined", "--shm-size=8g",
         "-v", f"{root}:{root}", "-v", f"{model}:{model}:ro",
         str(settings["IMAGE"]), "sleep", "infinity"],
        check=True, stdout=subprocess.DEVNULL,
    )
    (temporary / "container").write_text(container, encoding="utf-8")
    if settings.get("ANON_THP_MODE", "default") == "off":
        subprocess.run(
            ["docker", "exec", container, "gcc", "-O2", "-Wall", "-Wextra",
             str(runtime / "scripts/thp_exec.c"), "-o", str(runtime / "scripts/thp_exec")],
            check=True,
        )


if __name__ == "__main__":
    main()
