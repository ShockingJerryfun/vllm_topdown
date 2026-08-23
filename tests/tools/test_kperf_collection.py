# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import csv
from pathlib import Path

from pytest import approx

from scripts import build_xlsx, parse_run


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_time_parser_calculates_single_thread_utilization(tmp_path: Path) -> None:
    measurement = tmp_path / "measurement.log"
    measurement.write_text(
        "KPERF_TIME,add_requests,1,10000,6000,1\n",
        encoding="utf-8",
    )

    rows = parse_run.parse_rows(measurement, [], "time")

    assert rows["add_requests"] == [
        {
            "sequence": 1,
            "global_call": 1,
            "wall_time_us": 10,
            "thread_cpu_time_us": 6,
            "cpu_utilization": approx(0.6),
            "valid": 1,
        }
    ]


def test_pmu_parser_does_not_mix_wall_time_into_counter_rows(
    tmp_path: Path,
) -> None:
    measurement = tmp_path / "measurement.log"
    measurement.write_text(
        "KPERF,add_requests,1,100,100,1,7,8\n",
        encoding="utf-8",
    )

    row = parse_run.parse_rows(measurement, ["cycles", "instructions"], "pmu")[
        "add_requests"
    ][0]

    assert row == {
        "sequence": 1,
        "global_call": 1,
        "time_enabled": 100,
        "time_running": 100,
        "valid": 1,
        "cycles": 7,
        "instructions": 8,
    }
    assert "duration_us" not in row


def test_detail_rows_take_time_from_the_independent_time_run(
    tmp_path: Path,
) -> None:
    timing_row = {
        "sequence": 1,
        "global_call": 1,
        "wall_time_us": 12.5,
        "thread_cpu_time_us": 10,
        "cpu_utilization": 0.8,
        "valid": 1,
    }
    counter_row = {
        "sequence": 1,
        "global_call": 1,
        "time_enabled": 200,
        "time_running": 200,
        "valid": 1,
        "cycles": 123,
    }
    write_csv(tmp_path / "time" / "raw" / "add_requests.csv", [timing_row])
    write_csv(tmp_path / "base" / "raw" / "add_requests.csv", [counter_row])
    write_csv(tmp_path / "base" / "parsed" / "add_requests.csv", [counter_row])
    group = build_xlsx.GroupSpec(
        name="base",
        suffix="BASE",
        events=("cycles",),
        event_headers=("cycles",),
        semantic_headers=(),
        derived=(),
    )

    rows = build_xlsx.load_stage_rows(tmp_path, group, "add_requests")

    assert rows[0].wall_time_us == approx(12.5)
    assert rows[0].time_enabled == 200
    assert rows[0].counts == {"cycles": 123}
