# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import json
from collections import defaultdict
from pathlib import Path

from pytest import MonkeyPatch

from scripts.hygon_c86_7490 import summary

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "scripts" / "hygon_c86_7490" / "report_config.json"
RUN_PATH = REPO_ROOT / "scripts" / "hygon_c86_7490" / "run.sh"


def test_dcache_events_and_report_formulas() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    dcache = next(group for group in config["groups"] if group["name"] == "dcache")

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


def test_summary_keeps_request_and_demand_scopes_separate(
    monkeypatch: MonkeyPatch,
) -> None:
    row = defaultdict(
        lambda: "1",
        {
            "instructions": "1000",
            "l1i_fetch_misses": "10",
            "l2_request_activity": "500",
            "l2_demand_misses": "50",
            "l2_demand_hits": "150",
        },
    )
    monkeypatch.setattr(summary, "read_rows", lambda *_: [row])
    metrics = summary.stage_metrics(Path(), "add_requests")
    expected = {
        "L1I 32B fetch-window miss MPKI": 10,
        "L2 request activity MPKI": 500,
        "L2 demand access MPKI": 200,
        "L2 miss MPKI": 50,
        "L2 hit ratio": 0.75,
        "L2 miss ratio": 0.25,
    }
    assert {name: metrics[name] for name in expected} == expected
    metric_names = list(metrics)
    l2_start = metric_names.index("L2 request activity MPKI")
    assert metric_names[l2_start : l2_start + 5] == list(expected)[1:]
