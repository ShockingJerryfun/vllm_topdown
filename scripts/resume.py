# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Durable measurement-round checkpoints; interrupted files stay in history."""

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path

import regex as re

LOGGER = logging.getLogger(__name__)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def archive(path: Path) -> None:
    if not path.exists():
        return
    history = path.parent / ".history" / path.name
    history.mkdir(parents=True, exist_ok=True)
    number = 1
    while (history / str(number)).exists():
        number += 1
    path.rename(history / str(number))


def benchmark_ok(folder: Path) -> bool:
    path = folder / "benchmark.log"
    return (
        path.is_file()
        and re.search(r"Successful requests:\s+1\b", path.read_text()) is not None
    )


def checkpoint(folder: Path) -> None:
    if not benchmark_ok(folder):
        raise ValueError(f"Benchmark is incomplete: {folder}")
    names = ["benchmark.log", "run.env"]
    if folder.name == "hotspot":
        names += ["perf_report.txt"]
    elif folder.name == "frequency":
        names += ["summary.json", "frequency.csv"]
    else:
        names += ["measurement.log", "stop.json"]
    proofs = {name: digest(folder / name) for name in names}
    save(folder / "round_complete.json", {"files": proofs})


def ready(folder: Path) -> bool:
    receipt = folder / "round_complete.json"
    if receipt.exists():
        value = json.loads(receipt.read_text())
        if not value["files"] or any(
            not (folder / name).is_file() or digest(folder / name) != sha
            for name, sha in value["files"].items()
        ):
            raise ValueError(f"Completed round changed: {folder}")
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "commit", "archive"))
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    if args.action == "commit":
        checkpoint(args.folder)
    elif args.action == "archive":
        archive(args.folder)
    elif ready(args.folder):
        LOGGER.info("Retained completed round: %s", args.folder)
        raise SystemExit(10)
    else:
        archive(args.folder)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
