"""Prepare the existing vLLM collector layout for an isolated run."""

import json
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
LAYOUT = Path(__file__).with_name("compat_layout.json")


def prepare_runtime(runtime: Path) -> None:
    """Assemble the categorized scripts and link the vLLM adapter files."""
    runtime.mkdir(parents=True)
    layout: dict[str, str] = json.loads(LAYOUT.read_text(encoding="utf-8"))
    for old_path, new_path in layout.items():
        target = runtime / old_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / new_path, target)
    source = ROOT / "bench/infer/llm/vllm/overrides/vllm"
    shutil.copytree(source, runtime / "vllm", copy_function=os.symlink)
    (runtime / "kperf_instrument.py").symlink_to(
        ROOT / "collect/pmu/topdown/vllm/kperf_instrument.py"
    )
