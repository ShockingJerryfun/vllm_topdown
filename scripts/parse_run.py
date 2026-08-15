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
    r"(?P<stage>update_states|prepare_inputs|forward|compute_logits|sample|bookkeeping),"
    r"(?P<call>\d+),(?P<duration>\d+(?:\.\d+)?),(?P<counts>\d+(?:,\d+)*)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--event-codes", required=True)
    return parser.parse_args()


def parse_rows(
    path: Path, event_codes: list[str]
) -> dict[str, list[dict[str, str | int | float]]]:
    rows: dict[str, list[dict[str, str | int | float]]] = defaultdict(list)
    for line_number, raw_line in enumerate(
        path.read_text(errors="replace").splitlines(), 1
    ):
        match = ROW_PATTERN.search(ANSI_ESCAPE.sub("", raw_line))
        if match is None:
            continue
        counts = match.group("counts").split(",")
        if len(counts) != len(event_codes):
            raise ValueError(f"{path}:{line_number}: invalid counter count")
        stage = match.group("stage")
        row: dict[str, str | int | float] = {
            "sequence": len(rows[stage]) + 1,
            "global_call": int(match.group("call")),
            "duration_us": float(match.group("duration")),
        }
        row.update(zip(event_codes, (int(value) for value in counts), strict=True))
        rows[stage].append(row)
    return {stage: rows.get(stage, []) for stage in STAGES}


def validate(
    run_dir: Path,
    event_codes: list[str],
    rows: dict[str, list[dict[str, str | int | float]]],
) -> int:
    server_log = ANSI_ESCAPE.sub(
        "", (run_dir / "server.log").read_text(errors="replace")
    )
    if "[kperf] init failed" in server_log or "perf_event_open" in server_log:
        raise ValueError("kperf startup failed")
    enabled = [line for line in server_log.splitlines() if "[kperf] enabled:" in line]
    if not any(all(code in line for code in event_codes) for line in enabled):
        raise ValueError("kperf event list not enabled")
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
    event_codes: list[str],
    rows: dict[str, list[dict[str, str | int | float]]],
    aligned_rows: int,
) -> dict[str, int]:
    output_dir = run_dir / "parsed"
    output_dir.mkdir()
    headers = ["sequence", "global_call", "duration_us", *event_codes]
    valid_counts: dict[str, int] = {}
    for stage in STAGES:
        end = aligned_rows - 1 if stage == "update_states" else aligned_rows
        valid_rows = rows[stage][1:end]
        valid_counts[stage] = len(valid_rows)
        with (output_dir / f"{stage}.csv").open(
            "w", newline="", encoding="utf-8"
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(valid_rows)
    return valid_counts


def main() -> int:
    args = parse_args()
    event_codes = [code.strip().lower() for code in args.event_codes.split(",")]
    rows = parse_rows(args.run_dir / "measurement.log", event_codes)
    aligned_rows = validate(args.run_dir, event_codes, rows)
    counts = write_csvs(args.run_dir, event_codes, rows, aligned_rows)
    LOGGER.info("valid rows: %s", counts)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
