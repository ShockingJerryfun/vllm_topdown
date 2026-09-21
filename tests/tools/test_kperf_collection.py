# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import csv
import importlib
import json
import struct
from argparse import Namespace
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from pytest import MonkeyPatch, approx, fixture, mark, raises

import kperf_instrument
from scripts import build_xlsx, parse_run, switch_pmu


def write_csv(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
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


@fixture
def controlled_collector(monkeypatch: MonkeyPatch) -> None:
    for name in (
        "MODE",
        "EVENTS",
        "NAMES",
        "SCOPE",
        "PMU_NAME",
        "ENABLED",
        "OWNER_TID",
        "TARGET_STAGE",
        "QUALIFIER_STAGE",
        "CALL",
        "NAME",
        "QUALIFIED",
        "ACTIVE",
        "WALL_START_NS",
        "THREAD_START_NS",
        "WALL_OVERHEAD_NS",
        "THREAD_OVERHEAD_NS",
    ):
        monkeypatch.setattr(kperf_instrument, name, getattr(kperf_instrument, name))
    monkeypatch.setattr(kperf_instrument, "CONTROL_ENABLED", True)
    monkeypatch.setattr(kperf_instrument, "OWNER_TID", 0)
    monkeypatch.setattr(kperf_instrument, "ACTIVE", False)
    monkeypatch.setattr(kperf_instrument, "COUNTER_GROUPS", [])
    monkeypatch.setattr(kperf_instrument, "GROUP_TIMES", {})


def test_runtime_switch_closes_old_events_and_keeps_function_counting(
    monkeypatch: MonkeyPatch,
    controlled_collector: None,
) -> None:
    """Switching does not enable counters; begin/finish still own that boundary."""
    messages: list[str] = []
    operations: list[tuple] = []
    next_fd = iter(range(10, 20))

    def fake_open(*args, **kwargs):
        fd = next(next_fd)
        operations.append(("open", fd))
        return fd

    monkeypatch.setattr(kperf_instrument, "open_event", fake_open)
    monkeypatch.setattr(kperf_instrument, "event_id", lambda fd: fd + 100)
    monkeypatch.setattr(kperf_instrument, "emit", messages.append)
    monkeypatch.setattr(
        kperf_instrument.os, "close", lambda fd: operations.append(("close", fd))
    )
    monkeypatch.setattr(
        kperf_instrument.fcntl,
        "ioctl",
        lambda fd, op, flag: operations.append(("ioctl", fd, op, flag)),
    )
    first = kperf_instrument.configure("pmu", "0x11", "cycles", round_id="first")
    assert operations == [("open", 10)]
    assert first["event_ids"] == [[110]]
    totals = iter((100, 240))

    def fake_read(fd, size):
        total = next(totals)
        return struct.pack("<QQQQQ", 1, total, total, 17, 110)

    monkeypatch.setattr(kperf_instrument.os, "read", fake_read)
    for _ in range(2):
        kperf_instrument.kperf_begin("sample")
        kperf_instrument.kperf_finish("sample")
    assert messages[-2:] == [
        "KPERF,sample,1,100,100,1,17",
        "KPERF,sample,2,140,140,1,17",
    ]
    second = kperf_instrument.configure("pmu", "0x8", "instructions", round_id="second")
    assert second["tid"] == first["tid"]
    assert second["pid"] == first["pid"]
    assert operations[-2:] == [("close", 10), ("open", 11)]
    assert kperf_instrument.CALL == 0
    assert kperf_instrument.GROUP_TIMES == {}
    kperf_instrument.configure("disabled")
    assert not kperf_instrument.COUNTER_GROUPS
    assert not kperf_instrument.ENABLED
    count = len(messages)
    kperf_instrument.kperf_begin("sample")
    kperf_instrument.kperf_finish("sample")
    assert len(messages) == count


def test_runtime_switch_rejects_active_or_different_thread(
    monkeypatch: MonkeyPatch,
    controlled_collector: None,
) -> None:
    monkeypatch.setattr(kperf_instrument, "ACTIVE", True)
    with raises(RuntimeError, match="idle collector"):
        kperf_instrument.configure("disabled")
    monkeypatch.setattr(kperf_instrument, "ACTIVE", False)
    monkeypatch.setattr(kperf_instrument, "OWNER_TID", -1)
    with raises(RuntimeError, match="owner thread"):
        kperf_instrument.configure("time")


def test_runtime_switch_failure_does_not_collect_the_previous_group(
    monkeypatch: MonkeyPatch,
    controlled_collector: None,
) -> None:
    def fail_open(*args, **kwargs):
        raise OSError("unsupported event")

    monkeypatch.setattr(kperf_instrument, "open_event", fail_open)
    with raises(RuntimeError, match="Failed to open"):
        kperf_instrument.configure("pmu", "0xffff", "unsupported")
    assert not kperf_instrument.ENABLED
    assert not kperf_instrument.COUNTER_GROUPS


def test_switch_client_drains_then_resumes_only_after_matching_ack(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []
    worker = {"pid": 10, "tid": 12}

    def fake_post(url, payload, timeout):
        calls.append((url, payload))
        if url.endswith("collective_rpc"):
            params = payload["kwargs"]
            return {
                "results": [
                    {
                        **worker,
                        "round_id": params["round_id"],
                        "mode": params["mode"],
                        "events": [17] if params["mode"] == "pmu" else [],
                        "names": ["cycles"] if params["mode"] == "pmu" else [],
                    }
                ]
            }
        return {}

    monkeypatch.setattr(switch_pmu, "post", fake_post)
    monkeypatch.setenv("KPERF_MODE", "pmu")
    monkeypatch.setenv("KPERF_RAW_EVENTS", "0x11")
    monkeypatch.setenv("KPERF_EVENT_NAMES", "cycles")
    identity = tmp_path / "worker.json"
    switch_pmu.switch("http://local", "one", identity, False, 10)
    assert [url for url, _ in calls] == [
        "http://local/pause?mode=wait&clear_cache=false",
        "http://local/collective_rpc",
        "http://local/resume",
    ]
    calls.clear()
    switch_pmu.switch("http://local", "one", identity, True, 10)
    assert not any(url.endswith("resume") for url, _ in calls)
    calls.clear()
    worker["tid"] = 99
    with raises(RuntimeError, match="PID/TID changed"):
        switch_pmu.switch("http://local", "two", identity, False, 10)
    assert not any(url.endswith("resume") for url, _ in calls)


def test_target_span_ignores_nested_stages_and_marks_fullgraph(
    monkeypatch: MonkeyPatch,
) -> None:
    messages: list[str] = []
    wall_times = iter((100, 400))
    thread_times = iter((200, 500))
    stage = "execute_model_to_sample_tokens"
    monkeypatch.setattr(kperf_instrument, "ENABLED", True)
    monkeypatch.setattr(kperf_instrument, "MODE", "time")
    monkeypatch.setattr(kperf_instrument, "TARGET_STAGE", stage)
    monkeypatch.setattr(kperf_instrument, "QUALIFIER_STAGE", "run_fullgraph")
    monkeypatch.setattr(kperf_instrument, "ACTIVE", False)
    monkeypatch.setattr(kperf_instrument, "QUALIFIED", False)
    monkeypatch.setattr(kperf_instrument, "CALL", 0)
    monkeypatch.setattr(kperf_instrument, "WALL_OVERHEAD_NS", 0)
    monkeypatch.setattr(kperf_instrument, "THREAD_OVERHEAD_NS", 0)
    monkeypatch.setattr(
        kperf_instrument.time,
        "perf_counter_ns",
        lambda: next(wall_times),
    )
    monkeypatch.setattr(
        kperf_instrument.time,
        "thread_time_ns",
        lambda: next(thread_times),
    )
    monkeypatch.setattr(kperf_instrument, "emit", messages.append)

    kperf_instrument.kperf_span_begin(stage)
    kperf_instrument.kperf_begin("prepare_inputs")
    kperf_instrument.kperf_begin("run_fullgraph")
    kperf_instrument.kperf_finish("run_fullgraph")
    kperf_instrument.kperf_span_finish(stage)

    assert messages == [
        f"KPERF_TIME,{stage},1,300,300,1",
        f"KPERF_QUALIFIER,{stage},1,run_fullgraph",
    ]


def test_uncore_group_opens_systemwide_on_representative_cpu(
    monkeypatch: MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int, int, int, bool, bool]] = []

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
    default_rows: list[dict[str, str]] = [defaultdict(lambda: "1")]

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
        if module_name == "scripts.920b.summary":
            assert metrics["FrontendBound"] == approx(1 / 6)
            assert metrics["Fetch Latency Bound"] == approx(1)
            assert metrics["Fetch Bandwidth Bound"] == approx(-5 / 6)
            assert metrics["Idle by iCache Miss"] == approx(100)
            assert metrics["Idle by iTLB Miss"] == approx(100)
            assert metrics["Branch Flush"] == approx(5)
            assert metrics["OoO Flush"] == approx(9)
            assert metrics["SP Flush"] == approx(5)
            assert metrics["Flush"] == approx(19)
        else:
            assert metrics["FrontendBound"] == approx(1 / 8)
            assert metrics["Fetch Latency Bound"] == approx(1)
            assert metrics["Fetch Bandwidth Bound"] == approx(-7 / 8)
            assert metrics["Idle by iCache Miss"] == approx(1)
            assert metrics["Idle by iTLB Miss"] == approx(1)
            assert metrics["Branch Flush"] == approx(1)
            assert metrics["OoO Flush"] == approx(1)
            assert metrics["SP Flush"] == approx(1)
            assert metrics["Flush"] == approx(3)


def test_all_chip_summaries_keep_the_common_metric_order(
    monkeypatch: MonkeyPatch,
) -> None:
    expected = [
        metric for metric in build_xlsx.SUMMARY_METRICS if metric != "cycle占比"
    ]
    rows: list[dict[str, str]] = [defaultdict(lambda: "1")]

    for module_name in (
        "scripts.920b.summary",
        "scripts.950.summary",
        "scripts.hygon_c86_7490.summary",
    ):
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "read_rows", lambda *_args: rows)

        assert list(module.stage_metrics(Path(), "add_requests")) == expected


def test_arm_event_groups_match_report_configs() -> None:
    root = Path(__file__).resolve().parents[2]
    env_text = (root / "scripts" / "config.env").read_text(encoding="utf-8")

    expected = {
        "920b": {
            "topdown": ["0x0011", "0x0008", "0x2011", "0x2012", "0x001b"],
            "flush": ["0x0011", "0x0010", "0x2010", "0x104f", "0x200f"],
            "badspec_branch": [
                "0x0010",
                "0x1010",
                "0x1013",
                "0x1016",
                "0x100d",
            ],
            "backend_core": [
                "0x0011",
                "0x7001",
                "0x7002",
                "0x7003",
                "0x7004",
            ],
            "backend_memory": [
                "0x0011",
                "0x7005",
                "0x7006",
                "0x7007",
                "0x7008",
                "0x7009",
            ],
        },
        "950": {
            "topdown": ["0x0011", "0x0008", "0x1f21", "0x1f22", "0x001b"],
            "frontend_detail": [
                "0x0011",
                "0x1f10",
                "0x1f11",
                "0x1f12",
                "0x1f13",
                "0x1f14",
            ],
            "badspec_branch": [
                "0x0010",
                "0x2010",
                "0x1010",
                "0x1013",
                "0x1016",
                "0x100d",
            ],
            "backend_core": [
                "0x0011",
                "0x7000",
                "0x7001",
                "0x7002",
                "0x7003",
                "0x7004",
            ],
            "backend_memory": [
                "0x0011",
                "0x7005",
                "0x7006",
                "0x7007",
                "0x7008",
                "0x7009",
            ],
            "branch": ["0x0008", "0x0021", "0x0022", "0x203f", "0x2040"],
        },
    }
    for chip, groups_expected in expected.items():
        config_path = root / "scripts" / chip / "report_config.json"
        report_groups = build_xlsx.load_config(config_path)
        assert all(
            len(name) <= 31
            for name in build_xlsx.expected_sheet_names(report_groups, True)
        )
        config = json.loads(config_path.read_text(encoding="utf-8"))
        groups = {group["name"]: group for group in config["groups"]}
        assert all(len(group["events"]) <= 6 for group in config["groups"])
        summary = importlib.import_module(f"scripts.{chip}.summary")
        assert tuple(groups) == summary.GROUPS[1:]
        run_text = (root / "scripts" / chip / "run.sh").read_text(encoding="utf-8")
        for group_name, group in groups.items():
            env_name = f"EVENTS_{chip.upper()}_{group_name.upper()}"
            assert f"{env_name}={','.join(group['events'])}" in env_text
            assert run_text.count(f"{group_name}|${env_name}") == 2
        for group_name, events in groups_expected.items():
            assert groups[group_name]["events"] == events

    assert 'HOTSPOT_WORKER_PATTERN="VLLM::Worker_TP"' in env_text
    run_one = (root / "scripts" / "run_one.sh").read_text(encoding="utf-8")
    assert "pgrep" not in run_one
    assert 'placement.py" worker --api' in run_one


def test_arm_end_to_end_cycles_do_not_change_pipeline_shares() -> None:
    for module_name in ("scripts.920b.summary", "scripts.950.summary"):
        module = importlib.import_module(module_name)
        values = {
            stage: {"cycles": 100.0}
            for stage in (*module.STAGES, module.END_TO_END_STAGE)
        }
        values[module.END_TO_END_STAGE]["cycles"] = 10_000.0

        shares = module.pipeline_cycle_shares(values)

        assert set(shares) == set(module.STAGES)
        assert all(value == approx(0.125) for value in shares.values())


def test_arm_l2_l3_summary_formulas(monkeypatch: MonkeyPatch) -> None:
    common_rows = {
        "time": [defaultdict(lambda: "0", wall_time_us="10", thread_cpu_time_us="8")],
        "badspec_branch": [
            defaultdict(
                lambda: "0",
                **{
                    "0x0010": "30",
                    "0x2010": "10",
                    "0x1010": "6",
                    "0x1013": "3",
                    "0x1016": "6",
                    "0x100d": "3",
                },
            )
        ],
        "backend_memory": [
            defaultdict(
                lambda: "0",
                **{
                    "0x0011": "100",
                    "0x7005": "40",
                    "0x7006": "10",
                    "0x7007": "30",
                    "0x7008": "20",
                    "0x7009": "5",
                },
            )
        ],
        "branch": [
            defaultdict(
                lambda: "0",
                **{
                    "0x0008": "300",
                    "0x0021": "100",
                    "0x0022": "10",
                    "0x203f": "2",
                    "0x2040": "10",
                },
            )
        ],
    }
    defaults: list[dict[str, str]] = [defaultdict(lambda: "1")]

    for chip, slots in (("920b", 6), ("950", 8)):
        module = importlib.import_module(f"scripts.{chip}.summary")
        rows_by_group = dict(common_rows)
        rows_by_group["topdown"] = [
            defaultdict(
                lambda: "0",
                **{
                    "0x0011": "100",
                    "0x0008": str(50 * slots),
                    ("0x2011" if chip == "920b" else "0x1f21"): str(10 * slots),
                    ("0x2012" if chip == "920b" else "0x1f22"): "5",
                    "0x001b": str(60 * slots),
                },
            )
        ]
        rows_by_group["backend_core"] = [
            defaultdict(
                lambda: "0",
                **{
                    "0x0011": "100",
                    "0x7000": "6",
                    "0x7001": "80",
                    "0x7002": "5",
                    "0x7003": "4",
                    "0x7004": "3",
                },
            )
        ]
        if chip == "920b":
            rows_by_group["flush"] = [
                defaultdict(
                    lambda: "0",
                    **{
                        "0x0011": "100",
                        "0x0010": "30",
                        "0x2010": "10",
                        "0x104f": "2",
                        "0x200f": "2",
                    },
                )
            ]
        else:
            rows_by_group["frontend_detail"] = [
                defaultdict(
                    lambda: "0",
                    **{
                        "0x0011": "100",
                        "0x1f10": "1",
                        "0x1f11": "2",
                        "0x1f12": "3",
                        "0x1f13": "4",
                        "0x1f14": "5",
                    },
                )
            ]

        monkeypatch.setattr(
            module,
            "read_rows",
            lambda _root, group, _stage, rows=rows_by_group: rows.get(group, defaults),
        )
        metrics = module.stage_metrics(Path(), "add_requests")

        assert metrics["Retire"] == approx(0.5)
        assert metrics["FrontendBound"] == approx(0.1)
        assert metrics["BadSpec"] == approx(0.1)
        assert metrics["BackendBound"] == approx(0.3)
        assert metrics["Branch Mispredicts"] == approx(0.075)
        assert metrics["Machine Clears"] == approx(0.025)
        assert metrics["Indirect Branch"] == approx(0.2)
        assert metrics["Push Branch"] == approx(0.3)
        assert metrics["Pop Branch"] == approx(0.1)
        assert metrics["Other Branch"] == approx(0.6)
        assert metrics["Nuke Flush"] == approx(0.2)
        assert metrics["Other Flush"] == approx(0.8)
        assert metrics["Memory Bound"] == approx(0.5)
        assert metrics["Core Bound"] == approx(0.3)
        assert metrics["L1 Bound"] == approx(0.1)
        assert metrics["L2 Bound"] == approx(0.1)
        assert metrics["L3 Bound"] == approx(0.15)
        assert metrics["Mem Bound"] == approx(0.05)
        assert metrics["Store Bound"] == approx(0.1)
        if chip == "920b":
            assert metrics["Resource Bound"] is None
            assert metrics["Exe Ports Util"] == approx(0.18)
        else:
            assert metrics["Resource Bound"] == approx(0.06)
            assert metrics["Exe Ports Util"] == approx(0.12)


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


def test_end_to_end_parser_selects_only_fullgraph_decode_rows(
    tmp_path: Path,
) -> None:
    stage = parse_run.END_TO_END_STAGE
    measurement = tmp_path / "measurement.log"
    measurement.write_text(
        "\n".join(
            (
                f"KPERF_TIME,{stage},1,10000,9000,1",
                f"KPERF_QUALIFIER,{stage},2,run_fullgraph",
                f"KPERF_TIME,{stage},2,11000,10000,1",
                f"KPERF_QUALIFIER,{stage},3,run_fullgraph",
                f"KPERF_TIME,{stage},3,12000,11000,1",
            )
        ),
        encoding="utf-8",
    )
    rows = parse_run.parse_rows(measurement, [], "time", (stage,))
    qualifiers = parse_run.parse_qualifier_calls(
        measurement,
        stage,
        "run_fullgraph",
    )

    parse_run.write_csvs(
        tmp_path,
        [],
        rows,
        expected_calls=2,
        mode="time",
        profile="end_to_end",
        qualifier_calls=qualifiers,
    )

    with (tmp_path / "parsed" / f"{stage}.csv").open(
        newline="",
        encoding="utf-8",
    ) as handle:
        selected = list(csv.DictReader(handle))
    with (tmp_path / "collection_quality.csv").open(
        newline="",
        encoding="utf-8-sig",
    ) as handle:
        quality = list(csv.DictReader(handle))
    assert [row["global_call"] for row in selected] == ["2", "3"]
    assert quality == [
        {
            "stage": stage,
            "expected_selected": "2",
            "raw": "3",
            "selected": "2",
            "valid": "2",
            "invalid": "0",
            "status": "ok",
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


def test_end_to_end_detail_sheets_are_opt_in() -> None:
    group = build_xlsx.GroupSpec(
        name="topdown",
        suffix="TOPDOWN",
        events=("cycles",),
        event_headers=("cycles",),
        semantic_headers=(),
        derived=(),
    )

    assert build_xlsx.expected_sheet_names((group,))[-1] == "output TOPDOWN"
    assert build_xlsx.expected_sheet_names((group,), True)[-1] == (
        "execute_to_sample TOPDOWN"
    )


def test_frequency_benchmark_is_appended_to_summary(tmp_path: Path) -> None:
    summary_path = tmp_path / "summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["", "add_requests"])
        for metric in build_xlsx.SUMMARY_METRICS:
            writer.writerow([metric, "1"])

    benchmark = tmp_path / "frequency" / "benchmark.log"
    benchmark.parent.mkdir(parents=True)
    benchmark.write_text(
        "\n".join(
            (
                "========== Serving Benchmark Result ==========",
                "Successful requests:                     1",
                "Benchmark duration (s):                  2.50",
                "Request throughput (req/s):              0.40",
                "---------------Time to First Token---------------",
                "Mean TTFT (ms):                          12.34",
                "==================================================",
            )
        ),
        encoding="utf-8",
    )

    workbook = build_xlsx.Workbook()
    worksheet = workbook.active
    build_xlsx.write_summary(worksheet, tmp_path)
    labels = [worksheet.cell(row, 1).value for row in range(1, worksheet.max_row + 1)]
    header_row = labels.index("Benchmark") + 1
    assert worksheet.cell(header_row, 2).value == "数值"
    assert worksheet.cell(header_row + 1, 1).value == "Successful requests"
    assert worksheet.cell(header_row + 1, 2).value == 1
    assert worksheet.cell(header_row + 1, 2).number_format == "0"
    assert worksheet.cell(header_row + 2, 1).value == "Benchmark duration (s)"
    assert worksheet.cell(header_row + 2, 2).value == approx(2.5)
    assert worksheet.cell(header_row + 2, 2).number_format == "0.00"
    assert worksheet.cell(header_row + 4, 1).value == "Mean TTFT (ms)"
    workbook.close()


@mark.parametrize("end_only", [False, True])
def test_arm_workbooks_generate_with_frontend_metrics_and_benchmark(
    end_only: bool,
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    benchmark_text = "\n".join(
        (
            "========== Serving Benchmark Result ==========",
            "Successful requests:                     1",
            "Request throughput (req/s):              0.40",
            "==================================================",
        )
    )

    for chip in ("920b", "950"):
        run_root = tmp_path / chip
        config_path = root / "scripts" / chip / "report_config.json"
        groups = build_xlsx.load_config(config_path)
        summary = importlib.import_module(f"scripts.{chip}.summary")

        def write_profile(
            profile_root: Path,
            stages: tuple[str, ...],
            report_groups: tuple[build_xlsx.GroupSpec, ...],
        ) -> None:
            for call, stage in enumerate(stages, 1):
                timing = {
                    "sequence": 1,
                    "global_call": call,
                    "wall_time_us": 10,
                    "thread_cpu_time_us": 8,
                    "cpu_utilization": 0.8,
                    "valid": 1,
                }
                write_csv(profile_root / "time" / "raw" / f"{stage}.csv", [timing])
                write_csv(
                    profile_root / "time" / "parsed" / f"{stage}.csv",
                    [timing],
                )
                for group in report_groups:
                    counters = {
                        "sequence": 1,
                        "global_call": call,
                        "time_enabled": 100,
                        "time_running": 100,
                        "valid": 1,
                        **{event: 100 for event in group.events},
                    }
                    write_csv(
                        profile_root / group.name / "raw" / f"{stage}.csv",
                        [counters],
                    )
                    write_csv(
                        profile_root / group.name / "parsed" / f"{stage}.csv",
                        [counters],
                    )

        if not end_only:
            write_profile(run_root, summary.STAGES, groups)
        include_end_to_end = True
        write_profile(
            run_root / "end_to_end",
            (summary.END_TO_END_STAGE,),
            groups,
        )
        (run_root / "frequency").mkdir()
        (run_root / "frequency" / "benchmark.log").write_text(benchmark_text)
        if end_only:
            (run_root / "collection_profile").write_text("end_to_end\n")
        else:
            hotspot = run_root / "hotspot" / "perf_report.txt"
            hotspot.parent.mkdir(parents=True)
            hotspot.write_text("# Samples: 1\n100.00% worker\n")

        monkeypatch.setattr(
            summary,
            "parse_args",
            lambda run_root=run_root: Namespace(run_root=run_root),
        )
        monkeypatch.setattr(summary, "write_quality", lambda _root: None)
        assert summary.main() == 0

        args = Namespace(
            run_root=run_root,
            config=config_path,
            chip=chip,
            version="0.26",
            model_short="qwen3",
            input_len=7000,
            output_len=100,
            include_end_to_end=include_end_to_end,
        )
        output = build_xlsx.build_workbook(args, groups)
        workbook = build_xlsx.load_workbook(output, data_only=False)
        try:
            assert len(workbook.sheetnames) == 93
            assert workbook["汇总"]["B1"].value == summary.END_TO_END_STAGE
            assert workbook["汇总"]["C1"].value == summary.STAGES[0]
            labels = [
                workbook["汇总"].cell(row, 1).value
                for row in range(1, workbook["汇总"].max_row + 1)
            ]
            rows_by_label = {label: row for row, label in enumerate(labels, 1) if label}
            fetch_latency_label = build_xlsx.summary_display_label(
                "Fetch Latency Bound"
            )
            frontend_label = build_xlsx.summary_display_label("FrontendBound")
            itlb_label = build_xlsx.summary_display_label("Idle by iTLB Miss")
            branch_flush_label = build_xlsx.summary_display_label("Branch Flush")
            br_missrate_label = build_xlsx.summary_display_label("br missrate")
            assert labels.index(fetch_latency_label) == (
                labels.index("FrontendBound") + 1
            )
            assert labels.index("Benchmark") > labels.index("热点函数占比：")
            assert build_xlsx.summary_display_label("cycle占比") == "cycle占比"
            assert fetch_latency_label == "-- Fetch Latency Bound"
            assert itlb_label == "---- Idle by iTLB Miss"
            assert branch_flush_label == "------ Branch Flush"
            summary_sheet = workbook["汇总"]
            frontend_row = rows_by_label[frontend_label]
            latency_row = rows_by_label[fetch_latency_label]
            itlb_row = rows_by_label[itlb_label]
            branch_flush_row = rows_by_label[branch_flush_label]
            assert summary_sheet.cell(frontend_row, 1).fill.fgColor.rgb == "FFD9EAF7"
            assert summary_sheet.cell(frontend_row, 2).fill.fgColor.rgb == "FFD9EAF7"
            assert summary_sheet.cell(latency_row, 1).alignment.indent == 1
            assert summary_sheet.cell(latency_row, 1).fill.fgColor.rgb == "FFEDF4FA"
            assert summary_sheet.cell(itlb_row, 1).alignment.indent == 2
            assert summary_sheet.cell(branch_flush_row, 1).alignment.indent == 3
            assert (
                summary_sheet.cell(rows_by_label[br_missrate_label], 1).border.top.style
                == "medium"
            )
            benchmark_row = labels.index("Successful requests") + 1
            assert workbook["汇总"].cell(benchmark_row, 2).value == 1
            if end_only:
                assert workbook["汇总"]["C2"].value == "未采集"
                assert workbook["热点函数"]["A2"].value == "未采集（仅端到端模式）"
                assert (
                    workbook["add_requests TOPDOWN"]["A2"].value
                    == "未采集（仅端到端模式）"
                )
                assert workbook["execute_to_sample TOPDOWN"]["I2"].value == 100
            elif chip == "920b":
                detail = workbook["add_requests FLUSH"]
                assert detail["N2"].value == '=IFERROR(J2*5/I2,"")'
                assert detail["O2"].value == '=IFERROR(K2*9/I2,"")'
                assert detail["Q2"].value == '=IFERROR(N2+O2+P2,"")'
                assert detail["T2"].value == '=IFERROR(M2/K2,"")'
            else:
                detail = workbook["add_requests FRONTEND_DET"]
                assert detail["O2"].value == '=IFERROR(J2/I2,"")'
                assert detail["S2"].value == '=IFERROR(N2/I2,"")'
                assert detail["T2"].value == '=IFERROR(S2+Q2+R2,"")'
                branch_detail = workbook["add_requests BRANCH"]
                assert branch_detail["P2"].value == '=IFERROR(L2/M2,"")'
        finally:
            workbook.close()
