#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import logging
import re
from collections import defaultdict
from pathlib import Path

LOGGER = logging.getLogger(__name__)
STAGES = (
    "update_states",
    "prepare_inputs",
    "forward",
    "compute_logits",
    "sample",
    "bookkeeping",
)
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ROW_PATTERN = re.compile(
    r"KPERF_C86,(?P<stage>update_states|prepare_inputs|forward|compute_logits|sample|bookkeeping),"
    r"(?P<call>\d+),(?P<duration>\d+(?:\.\d+)?),(?P<enabled>\d+),"
    r"(?P<running>\d+),(?P<valid>[01]),(?P<counts>\d+(?:,\d+)*)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--event-names", required=True)
    return parser.parse_args()


def parse_rows(path: Path, names: list[str]) -> dict[str, list[dict[str, object]]]:
    rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    for line_number, raw_line in enumerate(
        path.read_text(errors="replace").splitlines(), 1
    ):
        match = ROW_PATTERN.search(ANSI_ESCAPE.sub("", raw_line))
        if match is None:
            continue
        counts = match.group("counts").split(",")
        if len(counts) != len(names):
            raise ValueError(f"{path}:{line_number}: invalid counter count")
        stage = match.group("stage")
        row: dict[str, object] = {
            "sequence": len(rows[stage]) + 1,
            "global_call": int(match.group("call")),
            "duration_us": float(match.group("duration")),
            "time_enabled": int(match.group("enabled")),
            "time_running": int(match.group("running")),
            "valid": int(match.group("valid")),
        }
        row.update(zip(names, (int(value) for value in counts), strict=True))
        rows[stage].append(row)
    return {stage: rows.get(stage, []) for stage in STAGES}


def validate(run_dir: Path, rows: dict[str, list[dict[str, object]]]) -> int:
    server_log = ANSI_ESCAPE.sub(
        "", (run_dir / "server.log").read_text(errors="replace")
    )
    if "[kperf-c86] init failed" in server_log:
        raise ValueError("PMU event group initialization failed")
    counts = {stage: len(stage_rows) for stage, stage_rows in rows.items()}
    if any(counts[stage] == 0 for stage in STAGES):
        raise ValueError(f"missing stage rows: {counts}")
    aligned = {counts[stage] for stage in STAGES[1:]}
    if len(aligned) != 1:
        raise ValueError(f"unaligned stage rows: {counts}")
    aligned_rows = aligned.pop()
    if aligned_rows != 100 or counts["update_states"] < aligned_rows:
        raise ValueError(f"unexpected stage rows: {counts}")
    return aligned_rows


def write_csvs(
    run_dir: Path,
    names: list[str],
    rows: dict[str, list[dict[str, object]]],
    aligned_rows: int,
) -> None:
    raw_dir = run_dir / "raw"
    parsed_dir = run_dir / "parsed"
    raw_dir.mkdir()
    parsed_dir.mkdir()
    headers = [
        "sequence",
        "global_call",
        "duration_us",
        "time_enabled",
        "time_running",
        "valid",
        *names,
    ]
    for stage in STAGES:
        for output_dir, selected in (
            (raw_dir, rows[stage]),
            (parsed_dir, rows[stage][1:aligned_rows]),
        ):
            with (output_dir / f"{stage}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerows(selected)


def main() -> int:
    args = parse_args()
    names = [name.strip() for name in args.event_names.split(",") if name.strip()]
    rows = parse_rows(args.run_dir / "measurement.log", names)
    aligned_rows = validate(args.run_dir, rows)
    write_csvs(args.run_dir, names, rows, aligned_rows)
    invalid = {
        stage: sum(int(row["valid"]) == 0 for row in stage_rows[1:aligned_rows])
        for stage, stage_rows in rows.items()
    }
    LOGGER.info("invalid rows: %s", invalid)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
