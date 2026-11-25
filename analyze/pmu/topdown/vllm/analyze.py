"""Generate the original Excel report and InfraPerf metric exports."""

import json
import logging
import os
import subprocess
from pathlib import Path

from analyze.latency.vllm import export_benchmark
from analyze.pmu.topdown.vllm.export import export_metrics


LOGGER = logging.getLogger(__name__)


def main() -> None:
    context = json.loads(Path(os.environ["INFRAPERF_CONTEXT"]).read_text())
    root = Path(context["root"])
    run_id = context["run_id"]
    temporary = Path(context["runtime"])
    runtime = temporary / "source"
    raw = root / "results/collect/pmu" / run_id
    analysis = root / "results/analyze/pmu" / run_id
    analysis.mkdir(parents=True)
    prebuilt = {"summary.csv", "collection_quality.csv", "details"}
    spe = str(context["settings"].get("SPE_ENABLE", 0)) in ("1", "True")
    for entry in raw.iterdir():
        if spe and (entry.name in prebuilt or entry.suffix == ".xlsx"):
            entry.rename(analysis / entry.name)
        else:
            (analysis / entry.name).symlink_to(
                os.path.relpath(entry, analysis), target_is_directory=entry.is_dir()
            )
    if not spe:
        container = (temporary / "container").read_text(encoding="utf-8")
        subprocess.run(
            ["docker", "exec", "-e", f"TOPDOWN_CONFIG={runtime / 'baseline.env'}",
             "-e", f"PROJECT={runtime}", container, "bash",
             str(root / "analyze/pmu/topdown/vllm/analyze.sh"), str(analysis)],
            check=True,
        )
    export_metrics(analysis, run_id, str(context["settings"]["CHIP"]))
    export_benchmark(root / "results/bench/infer/llm" / run_id,
                     root / "results/analyze/latency" / run_id)
    LOGGER.info("Excel: %s", next(analysis.glob("*.xlsx")))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
