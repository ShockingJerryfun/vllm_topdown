#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
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
ALIGNED_STAGES = STAGES[1:]
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
ROW_PATTERN = re.compile(
    r"(?P<stage>update_states|prepare_inputs|forward|compute_logits|sample|bookkeeping),"
    r"(?P<call>\d+),(?P<duration>\d+(?:\.\d+)?),(?P<counts>\d+(?:,\d+)*)\s*$"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Parse one vLLM kperf run.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--event-label", required=True)
    parser.add_argument("--event-codes", required=True)
    return parser.parse_args()


def parse_rows(
    log_path: Path, event_codes: list[str]
) -> dict[str, list[dict[str, object]]]:
    rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    malformed: list[str] = []
    for line_number, raw_line in enumerate(
        log_path.read_text(errors="replace").splitlines(), 1
    ):
        line = ANSI_ESCAPE.sub("", raw_line)
        match = ROW_PATTERN.search(line)
        if match is None:
            if any(f"{stage}," in line for stage in STAGES):
                malformed.append(f"{line_number}:{line}")
            continue
        counts = [int(value) for value in match.group("counts").split(",")]
        if len(counts) != len(event_codes):
            raise ValueError(
                f"{log_path}:{line_number}: expected {len(event_codes)} "
                f"counters, found {len(counts)}"
            )
        stage = match.group("stage")
        rows[stage].append(
            {
                "sequence": len(rows[stage]) + 1,
                "global_call": int(match.group("call")),
                "duration_us": float(match.group("duration")),
                "counters": dict(zip(event_codes, counts, strict=True)),
                "source_line": line_number,
            }
        )
    if malformed:
        raise ValueError("Malformed stage rows detected:\n" + "\n".join(malformed[:10]))
    return {stage: rows.get(stage, []) for stage in STAGES}


def validate_startup(server_log: Path, event_codes: list[str]) -> list[str]:
    text = ANSI_ESCAPE.sub("", server_log.read_text(errors="replace"))
    if "[kperf] init failed" in text or (
        "perf_event_open" in text and "failed:" in text
    ):
        raise ValueError(f"kperf startup failure found in {server_log}")
    enabled_lines = [line for line in text.splitlines() if "[kperf] enabled:" in line]
    expected_fds = len(event_codes)
    for line in enabled_lines:
        match = re.search(r"fds=\[([^]]*)\]", line)
        if match is None:
            continue
        fds = [value.strip() for value in match.group(1).split(",") if value.strip()]
        if len(fds) == expected_fds and all(code in line for code in event_codes):
            return enabled_lines
    raise ValueError(
        f"No kperf enabled line with {expected_fds} events in {server_log}"
    )


def validate_rows(
    rows: dict[str, list[dict[str, object]]], event_codes: list[str]
) -> tuple[int, list[str], dict[str, int]]:
    counts = {stage: len(stage_rows) for stage, stage_rows in rows.items()}
    missing = [stage for stage, count in counts.items() if count == 0]
    if missing:
        raise ValueError(f"No measurement rows for stages: {', '.join(missing)}")

    aligned_counts = {counts[stage] for stage in ALIGNED_STAGES}
    if len(aligned_counts) != 1:
        raise ValueError(f"Non-update stage counts are not aligned: {counts}")
    aligned_rows = aligned_counts.pop()
    if counts["update_states"] < aligned_rows:
        raise ValueError(f"update_states has fewer rows than other stages: {counts}")

    warnings: list[str] = []
    if aligned_rows != 100:
        warnings.append(f"Aligned row count is {aligned_rows}, not expected 100")
    extra_updates = counts["update_states"] - aligned_rows
    if extra_updates > 1:
        warnings.append(f"update_states has {extra_updates} trailing control rows")
    for stage, stage_rows in rows.items():
        calls = [int(row["global_call"]) for row in stage_rows]
        if calls != sorted(calls) or len(calls) != len(set(calls)):
            raise ValueError(f"Global call IDs are not strictly increasing for {stage}")
        for code in event_codes:
            total = sum(int(row["counters"][code]) for row in stage_rows)
            if total == 0:
                warnings.append(f"{stage} counter {code} is zero for every row")
    return aligned_rows, warnings, counts


def write_stage_csvs(
    run_dir: Path,
    rows: dict[str, list[dict[str, object]]],
    event_codes: list[str],
    aligned_rows: int,
) -> None:
    parsed_dir = run_dir / "parsed"
    parsed_dir.mkdir(exist_ok=True)
    headers = ["sequence", "global_call", "duration_us", *event_codes, "source_line"]
    for stage in STAGES:
        output_path = parsed_dir / f"{stage}.csv"
        with output_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in rows[stage][:aligned_rows]:
                writer.writerow(
                    {
                        "sequence": row["sequence"],
                        "global_call": row["global_call"],
                        "duration_us": row["duration_us"],
                        **row["counters"],
                        "source_line": row["source_line"],
                    }
                )

    with (parsed_dir / "update_states_extra.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows["update_states"][aligned_rows:]:
            writer.writerow(
                {
                    "sequence": row["sequence"],
                    "global_call": row["global_call"],
                    "duration_us": row["duration_us"],
                    **row["counters"],
                    "source_line": row["source_line"],
                }
            )


def main() -> int:
    args = parse_args()
    event_codes = [code.strip().lower() for code in args.event_codes.split(",")]
    if not event_codes or any(not code for code in event_codes):
        raise ValueError("event codes must be a non-empty comma-separated list")

    measurement_log = args.run_dir / "measurement.log"
    server_log = args.run_dir / "server.log"
    for path in (measurement_log, server_log, args.run_dir / "run.env"):
        if not path.is_file():
            raise FileNotFoundError(path)

    enabled_lines = validate_startup(server_log, event_codes)
    rows = parse_rows(measurement_log, event_codes)
    aligned_rows, warnings, counts = validate_rows(rows, event_codes)
    write_stage_csvs(args.run_dir, rows, event_codes, aligned_rows)

    result = {
        "version": args.version,
        "event_label": args.event_label,
        "event_codes": event_codes,
        "kperf_time_mode": "inner",
        "counts": counts,
        "aligned_rows": aligned_rows,
        "extra_update_rows": counts["update_states"] - aligned_rows,
        "kperf_enabled_lines": enabled_lines,
        "warnings": warnings,
        "rows": {
            stage: stage_rows[:aligned_rows] for stage, stage_rows in rows.items()
        },
        "update_states_extra": rows["update_states"][aligned_rows:],
    }
    (args.run_dir / "parsed.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    LOGGER.info(
        "parsed version=%s event=%s aligned_rows=%s counts=%s warnings=%s",
        args.version,
        args.event_label,
        aligned_rows,
        counts,
        warnings,
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
