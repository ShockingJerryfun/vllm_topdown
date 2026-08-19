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
    r"KPERF,(?P<stage>update_states|prepare_inputs|forward|compute_logits|sample|bookkeeping),"
    r"(?P<call>\d+),(?P<duration>\d+(?:\.\d+)?),(?P<enabled>\d+),"
    r"(?P<running>\d+),(?P<valid>[01]),(?P<counts>\d+(?:,\d+)*)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--event-names", required=True)
    parser.add_argument("--expected-calls", type=int, required=True)
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


def validate(
    run_dir: Path, names: list[str], rows: dict[str, list[dict[str, object]]]
) -> None:
    server_log = ANSI_ESCAPE.sub(
        "", (run_dir / "server.log").read_text(errors="replace")
    )
    if "[kperf] init failed" in server_log:
        raise ValueError("PMU event group initialization failed")
    enabled = [line for line in server_log.splitlines() if "[kperf] enabled:" in line]
    if not any(all(name in line for name in names) for line in enabled):
        raise ValueError("PMU event group was not enabled")
    if not any(rows.values()):
        raise ValueError("no stage rows found")


def selected_rows(
    stage: str,
    rows: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    stage_rows = rows[stage]
    if stage == "update_states" and rows["sample"]:
        stage_rows = stage_rows[: len(rows["sample"])]
    return stage_rows[1:]


def quality_status(
    stage: str,
    expected: int,
    raw_count: int,
    valid_count: int,
    invalid_count: int,
) -> str:
    if raw_count == 0:
        return "missing"
    if valid_count == 0:
        return "no_valid_rows"
    if invalid_count:
        return "invalid_rows"
    if stage == "update_states" and raw_count == expected + 1:
        return "extra_tail"
    if raw_count != expected:
        return "count_changed"
    return "ok"


def write_csvs(
    run_dir: Path,
    names: list[str],
    rows: dict[str, list[dict[str, object]]],
    expected_calls: int,
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
    quality: list[list[object]] = []
    for stage in STAGES:
        selected = selected_rows(stage, rows)
        for output_dir, output_rows in ((raw_dir, rows[stage]), (parsed_dir, selected)):
            with (output_dir / f"{stage}.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                writer.writerows(output_rows)
        valid_count = sum(int(row["valid"]) == 1 for row in selected)
        invalid_count = len(selected) - valid_count
        quality.append(
            [
                stage,
                expected_calls,
                len(rows[stage]),
                len(selected),
                valid_count,
                invalid_count,
                quality_status(
                    stage,
                    expected_calls,
                    len(rows[stage]),
                    valid_count,
                    invalid_count,
                ),
            ]
        )
    with (run_dir / "collection_quality.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "stage",
                "expected_raw",
                "raw",
                "selected",
                "valid",
                "invalid",
                "status",
            ]
        )
        writer.writerows(quality)


def main() -> int:
    args = parse_args()
    names = [name.strip() for name in args.event_names.split(",") if name.strip()]
    rows = parse_rows(args.run_dir / "measurement.log", names)
    validate(args.run_dir, names, rows)
    write_csvs(args.run_dir, names, rows, args.expected_calls)
    LOGGER.info("row counts: %s", {stage: len(rows[stage]) for stage in STAGES})
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
