"""Collect the selected vLLM workload's PMU, time and benchmark records."""

import json
import os
import subprocess
from pathlib import Path

from collect.pmu.topdown.vllm.results import organize_results


def main() -> None:
    context = json.loads(Path(os.environ["INFRAPERF_CONTEXT"]).read_text())
    root = Path(context["root"])
    temporary = Path(context["runtime"])
    runtime = temporary / "source"
    config = runtime / "baseline.env"
    raw = root / "results/collect/pmu" / context["run_id"]
    container = (temporary / "container").read_text(encoding="utf-8")
    settings = context["settings"]
    if str(settings.get("SPE_ENABLE", 0)) in ("1", "True"):
        command = ["bash", str(runtime / "scripts/run_experiment.sh"), str(config), str(raw)]
    else:
        command = ["docker", "exec", "-e", f"TOPDOWN_CONFIG={config}",
                   "-e", f"RUN_ROOT={raw}", "-e", "INFRAPERF_COLLECT_ONLY=1",
                   container, "bash", str(runtime / 'scripts' / settings['CHIP'] / 'run.sh')]
    subprocess.run(command, check=True)
    organize_results(root, context["run_id"])


if __name__ == "__main__":
    main()
