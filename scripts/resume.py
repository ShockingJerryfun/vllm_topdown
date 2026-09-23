# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Resume completed rounds and discard only an interrupted round."""

import argparse
import json
import shutil
from pathlib import Path

import regex as re


def benchmark_ok(folder: Path) -> bool:
    path = folder / "benchmark.log"
    return (
        path.is_file()
        and re.search(r"Successful requests:\s+1\b", path.read_text()) is not None
    )


def required_files(folder: Path) -> list[str]:
    names = ["benchmark.log", "run.env"]
    if folder.name == "hotspot":
        names.append("perf_report.txt")
    elif folder.name == "frequency":
        names.extend(("summary.json", "frequency.csv"))
    else:
        names.extend(("measurement.log", "stop.json"))
    return names


def checkpoint(folder: Path) -> None:
    if not benchmark_ok(folder):
        raise ValueError(f"Benchmark is incomplete: {folder}")
    missing = [name for name in required_files(folder) if not (folder / name).is_file()]
    if missing:
        raise ValueError(f"Round is incomplete: {folder}: {', '.join(missing)}")
    (folder / "round_complete.json").write_text(
        json.dumps({"status": "complete", "files": required_files(folder)}, indent=2)
        + "\n"
    )


def ready(folder: Path) -> bool:
    marker = folder / "round_complete.json"
    if not marker.is_file():
        return False
    try:
        value = json.loads(marker.read_text())
    except json.JSONDecodeError:
        return False
    return value.get("status") == "complete" and all(
        (folder / name).is_file() for name in required_files(folder)
    )


def prepare(folder: Path) -> bool:
    if ready(folder):
        return True
    if folder.exists():
        shutil.rmtree(folder)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "commit"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.action == "commit":
        checkpoint(args.folder)
    elif prepare(args.folder):
        raise SystemExit(10)


if __name__ == "__main__":
    main()
