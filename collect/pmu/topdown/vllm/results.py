"""Place vLLM collection outputs in the InfraPerf result directories."""

import os
import shutil
from pathlib import Path


def move_and_link(source: Path, target: Path) -> None:
    if not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    source.symlink_to(os.path.relpath(target, source.parent), target_is_directory=True)


def organize_results(root: Path, run_id: str) -> Path:
    """Separate time and benchmark records while retaining their run links."""
    raw = root / "results/collect/pmu" / run_id
    time = root / "results/collect/time" / run_id
    bench = root / "results/bench/infer/llm" / run_id
    for name in ("time", "end_to_end/time"):
        move_and_link(raw / name, time / name)
    for name in ("benchmark_before", "benchmark_after", "service"):
        move_and_link(raw / name, bench / name)
    return raw
