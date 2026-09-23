#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from __future__ import annotations

import argparse
import csv
import logging
from collections import defaultdict
from pathlib import Path

import regex as re

LOGGER = logging.getLogger(__name__)
PIPELINE_STAGES = (
    "add_requests",
    "prepare_inputs",
    "prepare_attn_runner",
    "prepare_attn_model_state",
    "run_fullgraph",
    "sample",
    "async_output_init",
    "postprocess_sampled",
)
END_TO_END_STAGE = "execute_model_to_sample_tokens"
STAGES = PIPELINE_STAGES
ALL_STAGES = (*PIPELINE_STAGES, END_TO_END_STAGE)
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
PMU_ROW_PATTERN = re.compile(
    r"KPERF,(?P<stage>" + "|".join(map(re.escape, ALL_STAGES)) + r"),"
    r"(?P<call>\d+),(?P<enabled>\d+),(?P<running>\d+),"
    r"(?P<valid>[01]),(?P<counts>\d+(?:,\d+)*)\s*$"
)
TIME_ROW_PATTERN = re.compile(
    r"KPERF_TIME,(?P<stage>" + "|".join(map(re.escape, ALL_STAGES)) + r"),"
    r"(?P<call>\d+),(?P<wall_ns>\d+),(?P<thread_ns>\d+),"
    r"(?P<valid>[01])\s*$"
)
QUALIFIER_PATTERN = re.compile(
    r"KPERF_QUALIFIER,(?P<stage>"
    + "|".join(map(re.escape, ALL_STAGES))
    + r"),(?P<call>\d+),(?P<qualifier>[^,\s]+)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--event-names", default="")
    parser.add_argument("--mode", choices=("pmu", "time"), default="pmu")
    parser.add_argument(
        "--profile",
        choices=("pipeline", "end_to_end"),
        default="pipeline",
    )
    parser.add_argument("--expected-calls", type=int, required=True)
    return parser.parse_args()


def parse_rows(
    path: Path,
    names: list[str],
    mode: str,
    stages: tuple[str, ...] = STAGES,
) -> dict[str, list[dict[str, object]]]:
    rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    for line_number, raw_line in enumerate(
        path.read_text(errors="replace").splitlines(), 1
    ):
        pattern = TIME_ROW_PATTERN if mode == "time" else PMU_ROW_PATTERN
        match = pattern.search(ANSI_ESCAPE.sub("", raw_line))
        if match is None:
            continue
        stage = match.group("stage")
        row: dict[str, object] = {
            "sequence": len(rows[stage]) + 1,
            "global_call": int(match.group("call")),
            "valid": int(match.group("valid")),
        }
        if mode == "time":
            wall_time_us = int(match.group("wall_ns")) / 1000
            thread_cpu_time_us = int(match.group("thread_ns")) / 1000
            row.update(
                {
                    "wall_time_us": wall_time_us,
                    "thread_cpu_time_us": thread_cpu_time_us,
                    "cpu_utilization": (
                        thread_cpu_time_us / wall_time_us if wall_time_us > 0 else ""
                    ),
                }
            )
        else:
            counts = match.group("counts").split(",")
            if len(counts) != len(names):
                raise ValueError(f"{path}:{line_number}: invalid counter count")
            row.update(
                {
                    "time_enabled": int(match.group("enabled")),
                    "time_running": int(match.group("running")),
                }
            )
            row.update(zip(names, (int(value) for value in counts), strict=True))
        rows[stage].append(row)
    return {stage: rows.get(stage, []) for stage in stages}


def parse_qualifier_calls(path: Path, stage: str, qualifier: str) -> list[int]:
    calls: list[int] = []
    for raw_line in path.read_text(errors="replace").splitlines():
        match = QUALIFIER_PATTERN.search(ANSI_ESCAPE.sub("", raw_line))
        if (
            match is not None
            and match.group("stage") == stage
            and match.group("qualifier") == qualifier
        ):
            calls.append(int(match.group("call")))
    return calls


def validate(
    run_dir: Path,
    names: list[str],
    rows: dict[str, list[dict[str, object]]],
    mode: str,
) -> None:
    server_log = ANSI_ESCAPE.sub(
        "", (run_dir / "server.log").read_text(errors="replace")
    )
    if "[kperf] init failed" in server_log:
        raise ValueError(f"{mode} collection initialization failed")
    enabled = [
        line
        for line in server_log.splitlines()
        if f"[kperf] enabled: mode={mode}" in line
    ]
    if not enabled:
        raise ValueError(f"{mode} collection was not enabled")
    if mode == "pmu" and not any(
        all(name in line for name in names) for line in enabled
    ):
        raise ValueError("PMU event group names do not match")
    if not any(rows.values()):
        raise ValueError("no stage rows found")


def select_decode_rows(
    rows: dict[str, list[dict[str, object]]],
) -> dict[str, list[dict[str, object]]]:
    ordered = sorted(
        (
            (int(row["global_call"]), stage, row)
            for stage in STAGES
            for row in rows[stage]
        ),
        key=lambda item: item[0],
    )
    anchor_offset = STAGES.index("run_fullgraph")
    selected: dict[str, list[dict[str, object]]] = {stage: [] for stage in STAGES}
    for index, (_, stage, _) in enumerate(ordered):
        if stage != "run_fullgraph":
            continue
        start = index - anchor_offset
        if start < 0:
            continue
        window = ordered[start : start + len(STAGES)]
        if [item[1] for item in window] != list(STAGES):
            continue
        calls = [item[0] for item in window]
        if calls != list(range(calls[0], calls[0] + len(STAGES))):
            continue
        for _, window_stage, row in window:
            selected_row = dict(row)
            selected_row["sequence"] = len(selected[window_stage]) + 1
            selected[window_stage].append(selected_row)
    return selected


def select_end_to_end_rows(
    rows: dict[str, list[dict[str, object]]],
    qualifier_calls: list[int],
) -> dict[str, list[dict[str, object]]]:
    qualified = set(qualifier_calls)
    selected: list[dict[str, object]] = []
    for row in rows[END_TO_END_STAGE]:
        if int(row["global_call"]) not in qualified:
            continue
        selected_row = dict(row)
        selected_row["sequence"] = len(selected) + 1
        selected.append(selected_row)
    return {END_TO_END_STAGE: selected}


def quality_status(
    expected: int,
    raw_count: int,
    selected_count: int,
    valid_count: int,
    invalid_count: int,
    expected_raw: int | None = None,
    qualifier_count: int | None = None,
) -> str:
    if raw_count == 0:
        return "missing"
    if selected_count == 0:
        return "no_decode_rows"
    if expected_raw is not None and raw_count != expected_raw:
        return "raw_count_changed"
    if qualifier_count is not None and qualifier_count != expected:
        return "qualifier_count_changed"
    if invalid_count:
        return "invalid_rows"
    if selected_count != expected or valid_count != expected:
        return "count_changed"
    return "ok"


def write_csvs(
    run_dir: Path,
    names: list[str],
    rows: dict[str, list[dict[str, object]]],
    expected_calls: int,
    mode: str,
    profile: str = "pipeline",
    qualifier_calls: list[int] | None = None,
) -> None:
    raw_dir = run_dir / "raw"
    parsed_dir = run_dir / "parsed"
    raw_dir.mkdir(exist_ok=True)
    parsed_dir.mkdir(exist_ok=True)
    if profile == "end_to_end":
        stages = (END_TO_END_STAGE,)
        calls = qualifier_calls or []
        decode_rows = select_end_to_end_rows(rows, calls)
        expected_raw = expected_calls + 1
        qualifier_count = len(calls)
    else:
        stages = STAGES
        decode_rows = select_decode_rows(rows)
        expected_raw = None
        qualifier_count = None
    headers = ["sequence", "global_call"]
    if mode == "time":
        headers.extend(
            ["wall_time_us", "thread_cpu_time_us", "cpu_utilization", "valid"]
        )
    else:
        headers.extend(["time_enabled", "time_running", "valid", *names])
    quality: list[list[object]] = []
    for stage in stages:
        selected = decode_rows[stage]
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
                    expected_calls,
                    len(rows[stage]),
                    len(selected),
                    valid_count,
                    invalid_count,
                    expected_raw,
                    qualifier_count,
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
                "expected_selected",
                "raw",
                "selected",
                "valid",
                "invalid",
                "status",
            ]
        )
        writer.writerows(quality)

    failures = [row for row in quality if row[-1] != "ok"]
    if failures:
        raise ValueError(f"decode stage validation failed: {failures}")


def main() -> int:
    args = parse_args()
    names = [name.strip() for name in args.event_names.split(",") if name.strip()]
    if args.mode == "pmu" and not names:
        raise ValueError("PMU mode requires --event-names")
    stages = (END_TO_END_STAGE,) if args.profile == "end_to_end" else STAGES
    measurement = args.run_dir / "measurement.log"
    rows = parse_rows(measurement, names, args.mode, stages)
    qualifier_calls = None
    if args.profile == "end_to_end":
        qualifier_calls = parse_qualifier_calls(
            measurement,
            END_TO_END_STAGE,
            "run_fullgraph",
        )
    validate(args.run_dir, names, rows, args.mode)
    write_csvs(
        args.run_dir,
        names,
        rows,
        args.expected_calls,
        args.mode,
        args.profile,
        qualifier_calls,
    )
    LOGGER.info("row counts: %s", {stage: len(rows[stage]) for stage in stages})
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
