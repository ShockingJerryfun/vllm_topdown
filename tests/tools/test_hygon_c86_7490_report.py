# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import json
from argparse import Namespace
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from pytest import MonkeyPatch, approx

from scripts import build_xlsx
from scripts.hygon_c86_7490 import summary

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "scripts" / "hygon_c86_7490" / "report_config.json"
RUN_PATH = REPO_ROOT / "scripts" / "hygon_c86_7490" / "run.sh"


def test_dcache_events_and_report_formulas() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dcache = next(group for group in config["groups"] if group["name"] == "dcache")
    frontend = next(group for group in config["groups"] if group["name"] == "frontend")

    assert dcache["events"] == [
        "instructions",
        "l1d_accesses",
        "l2_request_activity",
        "l2_demand_misses",
        "l2_demand_hits",
    ]
    assert dcache["event_headers"] == [
        "instructions",
        "L1D access",
        "L2 request activity",
        "L2 demand miss",
        "L2 demand hit",
    ]
    assert {metric["name"]: metric["formula"] for metric in dcache["derived"]} == {
        "L1D accesses / inst": '=IFERROR({l1d_accesses}/{instructions},"")',
        "L2 request activity MPKI": (
            '=IFERROR({l2_request_activity}/{instructions}*1000,"")'
        ),
        "L2 demand access MPKI": (
            '=IFERROR(({l2_demand_hits}+{l2_demand_misses})/{instructions}*1000,"")'
        ),
        "L2 miss MPKI": '=IFERROR({l2_demand_misses}/{instructions}*1000,"")',
        "L2 hit ratio": (
            '=IFERROR({l2_demand_hits}/({l2_demand_hits}+{l2_demand_misses}),"")'
        ),
        "L2 miss ratio": (
            '=IFERROR({l2_demand_misses}/({l2_demand_hits}+{l2_demand_misses}),"")'
        ),
    }
    run_line = (
        "dcache|0xc0,0x40,0xe860,0x0864,0xf064|"
        "instructions,l1d_accesses,l2_request_activity,"
        "l2_demand_misses,l2_demand_hits"
    )
    assert run_line in RUN_PATH.read_text(encoding="utf-8").splitlines()
    frontend_metric_names = [metric["name"] for metric in frontend["derived"]]
    assert "L1I 32B fetch-window miss MPKI" in frontend_metric_names
    assert "L1I fetch MPKI" not in frontend_metric_names
    assert "L2 coverage" not in CONFIG_PATH.read_text(encoding="utf-8")


def test_hygon_summary_uses_only_comparable_920b_metrics(
    monkeypatch: MonkeyPatch,
) -> None:
    row = defaultdict(
        lambda: "1",
        {
            "wall_time_us": "100",
            "thread_cpu_time_us": "50",
            "cycles": "2000",
            "instructions": "1000",
            "branches": "100",
            "branch_misses": "10",
            "l2_demand_misses": "50",
            "l2_demand_hits": "150",
            "l1_dtlb_misses": "25",
            "dtlb_l2_hits": "15",
            "dtlb_l2_misses": "5",
        },
    )
    monkeypatch.setattr(summary, "read_rows", lambda *_: [row])
    metrics = summary.stage_metrics(Path(), "add_requests")
    expected = {
        "CPU利用率": 0.5,
        "time(us)": 100,
        "cycles": 2000,
        "instructions": 1000,
        "br missrate": 0.1,
        "br mpki": 10,
        "L2d missrate": 0.25,
        "L2d mpki": 50,
        "dtlb mpki": 25,
        "stlb missrate": 0.25,
        "stlb mpki": 5,
    }
    assert {name: metrics[name] for name in expected} == expected
    assert metrics["IPC/Retire"] is None
    assert metrics["FrontendBound"] is None
    expected_labels = [
        label
        for label in build_xlsx.SUMMARY_METRICS
        if label != summary.CYCLES_SHARE_METRIC
    ]
    assert list(metrics) == expected_labels


def test_summary_places_cycles_share_below_cycles(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    stage_cycles = {
        stage: float(index) for index, stage in enumerate(summary.STAGES, start=1)
    }

    def fake_read_rows(_root: Path, _group: str, stage: str) -> list[dict[str, str]]:
        return [
            defaultdict(
                lambda: "1",
                {
                    "wall_time_us": "100",
                    "thread_cpu_time_us": "50",
                    "cycles": str(stage_cycles[stage]),
                    "instructions": "1000",
                },
            )
        ]

    monkeypatch.setattr(summary, "parse_args", lambda: Namespace(run_root=tmp_path))
    monkeypatch.setattr(summary, "read_rows", fake_read_rows)
    monkeypatch.setattr(summary, "write_quality", lambda _root: None)

    assert summary.main() == 0

    workbook = Workbook()
    worksheet = workbook.active
    build_xlsx.write_summary(worksheet, tmp_path)
    labels = [worksheet.cell(row, 1).value for row in range(1, worksheet.max_row + 1)]
    metric_labels = [label for label in labels if label in build_xlsx.SUMMARY_METRICS]
    assert metric_labels == list(build_xlsx.SUMMARY_METRICS)
    cycles_row = labels.index("cycles") + 1
    share_row = labels.index(summary.CYCLES_SHARE_METRIC) + 1
    assert share_row == cycles_row + 1
    cpu_row = labels.index("CPU利用率") + 1
    time_row = labels.index("time(us)") + 1
    frontend_row = labels.index("FrontendBound") + 1
    assert worksheet.cell(cpu_row, 2).value == approx(0.5)
    assert worksheet.cell(cpu_row, 2).number_format == "0.00%"
    assert worksheet.cell(time_row, 2).value == approx(100)
    assert worksheet.cell(frontend_row, 2).value == "未采集"

    total_cycles = sum(stage_cycles.values())
    for column, stage in enumerate(summary.STAGES, start=2):
        cell = worksheet.cell(share_row, column)
        expected = round(stage_cycles[stage] / total_cycles, 4)
        assert cell.value == approx(expected)
        assert cell.number_format == "0.00%"

    workbook.close()
