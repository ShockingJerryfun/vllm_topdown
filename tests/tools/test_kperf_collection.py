# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import csv
import importlib
import struct
from collections import defaultdict
from pathlib import Path

from pytest import MonkeyPatch, approx

import kperf_instrument
from scripts import build_xlsx, parse_run


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_uncore_cpumask_parser_keeps_each_representative_cpu() -> None:
    assert kperf_instrument.parse_cpu_list("0,8,16-17,8\n") == [0, 8, 16, 17]


def test_group_reader_preserves_requested_event_order(
    monkeypatch: MonkeyPatch,
) -> None:
    payload = struct.pack("<QQQQQQQ", 2, 100, 100, 7, 22, 5, 11)
    monkeypatch.setattr(kperf_instrument.os, "read", lambda _fd, _size: payload)

    assert kperf_instrument.read_group([3, 4], [11, 22]) == (100, 100, [5, 7])


def test_uncore_group_opens_systemwide_on_representative_cpu(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, int, bool, bool]] = []

    def fake_open_event(
        event: int,
        event_type: int,
        pid: int,
        cpu: int,
        group_fd: int,
        leader: bool,
        exclude_hv: bool,
    ) -> int:
        calls.append((event, event_type, pid, cpu, group_fd, leader, exclude_hv))
        return 10 + len(calls) - 1

    monkeypatch.setattr(kperf_instrument, "EVENTS", [0xFF04, 0x0106])
    monkeypatch.setattr(kperf_instrument, "open_event", fake_open_event)
    monkeypatch.setattr(kperf_instrument, "event_id", lambda fd: fd + 100)

    assert kperf_instrument.open_group(9, -1, 8, False) == (
        [10, 11],
        [110, 111],
    )
    assert calls == [
        (0xFF04, 9, -1, 8, -1, True, False),
        (0x0106, 9, -1, 8, 10, False, False),
    ]


def test_shared_summaries_use_aggregate_ratios(
    monkeypatch: MonkeyPatch,
) -> None:
    rows_by_group: dict[str, list[dict[str, str]]] = {
        "time": [
            defaultdict(lambda: "1", wall_time_us="1", thread_cpu_time_us="1"),
            defaultdict(lambda: "1", wall_time_us="9", thread_cpu_time_us="0"),
        ],
        "branch": [
            defaultdict(lambda: "1", **{"0x0008": "1", "0x0021": "1", "0x0022": "1"}),
            defaultdict(lambda: "1", **{"0x0008": "9", "0x0021": "9", "0x0022": "1"}),
        ],
    }
    default_rows = [defaultdict(lambda: "1")]

    for module_name in ("scripts.920b.summary", "scripts.950.summary"):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(
            module,
            "read_rows",
            lambda _root, group, _stage: rows_by_group.get(group, default_rows),
        )
        metrics = module.stage_metrics(Path(), "add_requests")

        assert metrics["CPU利用率"] == approx(0.1)
        assert metrics["频率(MHz)"] == approx(0.2)
        assert metrics["br missrate"] == approx(0.2)
        assert metrics["br mpki"] == approx(200)
        assert "IPC" in metrics
        assert "Retire" in metrics


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
