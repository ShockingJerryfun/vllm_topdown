"""Prepare the standalone Topdown vLLM adapter layout."""

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
LAYOUT = Path(__file__).with_name("probe_layout.json")


def prepare_runtime(runtime: Path) -> None:
    layout: dict[str, str] = json.loads(LAYOUT.read_text(encoding="utf-8"))
    for old_path, new_path in layout.items():
        target = runtime / old_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / new_path, target)
    (runtime / "topdown").symlink_to(ROOT / "collect/pmu/topdown", target_is_directory=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    prepare_runtime(args.runtime)


if __name__ == "__main__":
    main()
