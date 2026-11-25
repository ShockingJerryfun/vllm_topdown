#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from __future__ import annotations

import argparse
import csv
import logging
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)
STAGES = (
    "add_requests",
    "prepare_inputs",
    "prepare_attn_runner",
    "prepare_attn_model_state",
    "run_fullgraph",
    "sample",
    "async_output_init",
    "postprocess_sampled",
)
GROUPS = (
    "time",
    "topdown",
    "branch",
    "spec_ls",
    "spec_ase",
    "icache",
    "dcache",
    "tlb",
    "branch_detail",
    "frontend_detail",
    "backend_detail",
    "memory_detail",
    "l3",
)
CYCLES_SHARE_METRIC = "cycle占比"
UNSUPPORTED = "未支持"
INVALID = "无效"
PROXY_SUM_LIMIT = 1.05
ZEN1_GROUPS = ("branch_detail", "frontend_detail", "backend_detail", "memory_detail")
DIAGNOSTIC_NOTES = {
    "Return missrate": "近返回分支预测错误 / 退休近返回分支",
    "IC DQ empty": "DQ空导致IC管线停顿；不等同Fetch Latency Bound",
    "IC backpressure": "IC管线背压停顿；不等同Fetch Bandwidth Bound",
    "ALU token stall": "ALU token不足导致分派停顿；各资源项不可直接相加",
    "ALSQ token stall": "ALSQ token不足导致分派停顿；各资源项不可直接相加",
    "Retire token stall": "退休token不足导致分派停顿；各资源项不可直接相加",
    "DIV busy": "除法器忙周期；不等同DIV Stall",
    "L2 fill pending": "L2填充请求在途周期；不等同L2 Bound或Memory Bound",
    "iTLB MPKI（候选）": "历史84/85事件不响应；零计数不能证明无iTLB miss",
}
MetricValue = float | str | None
PERCENT_METRICS = {
    *(name for name in DIAGNOSTIC_NOTES if name != "iTLB MPKI（候选）"),
    "CPU利用率",
    CYCLES_SHARE_METRIC,
    "Retire",
    "FrontendBound",
    "Fetch Latency Bound",
    "Fetch Bandwidth Bound",
    "Idle by iCache Miss",
    "Idle by iTLB Miss",
    "Flush",
    "Branch Flush",
    "OoO Flush",
    "SP Flush",
    "Branch Mispredicts",
    "Indirect Branch",
    "Push Branch",
    "Pop Branch",
    "Other Branch",
    "Machine Clears",
    "Nuke Flush",
    "Other Flush",
    "BadSpec",
    "BackendBound",
    "Core Bound",
    "Resource Bound",
    "FDIV Stall",
    "DIV Stall",
    "FSU Stall",
    "Exe Ports Util",
    "Memory Bound",
    "L1 Bound",
    "L2 Bound",
    "L3 Bound",
    "Mem Bound",
    "Store Bound",
    "dp_spec",
    "ld_spec",
    "st_spec",
    "branch_spec",
    "ase_spec",
    "br missrate",
    "l1i missrate",
    "l2i missrate",
    "l1d missrate",
    "L2d missrate",
    "L3 missrate",
    "itlb missrate",
    "dtlb missrate",
    "stlb missrate",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    return parser.parse_args()


def read_rows(root: Path, group: str, stage: str) -> list[dict[str, str]]:
    path = root / group / "parsed" / f"{stage}.csv"
    # Old captures have no Zen1 detail groups; absent data must not become zero.
    if group in ZEN1_GROUPS and not (root / group).exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row["valid"] == "1"]


def mean(
    rows: list[dict[str, str]], value: Callable[[dict[str, str]], float]
) -> float | None:
    values = [value(row) for row in rows]
    return sum(values) / len(values) if values else None


def total(rows: list[dict[str, str]], fields: tuple[str, ...]) -> float:
    return sum(sum(float(row[field]) for field in fields) for row in rows)


def aggregate_ratio(
    rows: list[dict[str, str]],
    numerator: tuple[str, ...],
    denominator: tuple[str, ...],
    scale: float = 1.0,
) -> float | None:
    denominator_sum = total(rows, denominator)
    if denominator_sum <= 0:
        return None
    return scale * total(rows, numerator) / denominator_sum


def cross_group_ratio(
    numerator_rows: list[dict[str, str]],
    numerator: tuple[str, ...],
    denominator_rows: list[dict[str, str]],
    denominator: tuple[str, ...],
    scale: float = 1.0,
) -> float | None:
    denominator_sum = total(denominator_rows, denominator)
    if denominator_sum <= 0:
        return None
    return scale * total(numerator_rows, numerator) / denominator_sum


def average_frequency_mhz(
    cycles: float | None,
    time_us: float | None,
) -> float | None:
    if cycles is None or time_us is None or time_us <= 0:
        return None
    return cycles / time_us


def topdown_metrics(rows: list[dict[str, str]]) -> dict[str, MetricValue]:
    retire = aggregate_ratio(rows, ("retired_uops",), ("cycles",), 1 / 6)
    frontend = aggregate_ratio(rows, ("frontend_stall_any",), ("cycles",))
    cycles = total(rows, ("cycles",))
    bad_spec = None
    if cycles > 0:
        bad_uops = max(
            total(rows, ("dispatched_uops",)) - total(rows, ("retired_uops",)),
            0,
        )
        bad_spec = bad_uops / (6 * cycles)

    values: dict[str, MetricValue] = {
        "Retire": retire,
        "FrontendBound": frontend,
        "BadSpec": bad_spec,
        "BackendBound": None,
    }
    parts = (retire, frontend, bad_spec)
    if all(value is not None for value in parts):
        proxy_sum = sum(value for value in parts if value is not None)
        if proxy_sum > PROXY_SUM_LIMIT:
            return {metric: INVALID for metric in values}
        values["BackendBound"] = 1 - proxy_sum
    return values


def spec_metrics(
    spec_ls: list[dict[str, str]],
    spec_ase: list[dict[str, str]],
) -> dict[str, MetricValue]:
    ld_spec = aggregate_ratio(spec_ls, ("load_ops",), ("dispatched_uops",))
    st_spec = aggregate_ratio(
        spec_ls,
        ("store_ops", "load_store_ops"),
        ("dispatched_uops",),
    )
    # Keep the existing dp_spec proxy unchanged; branch_spec is computed separately.
    branch_uop_proxy = aggregate_ratio(spec_ls, ("branches",), ("dispatched_uops",))
    ase_spec = aggregate_ratio(
        spec_ase,
        ("fpu_spec_uops",),
        ("dispatched_uops",),
    )
    values: dict[str, MetricValue] = {
        "dp_spec": None,
        "ld_spec": ld_spec,
        "st_spec": st_spec,
        "ase_spec": ase_spec,
    }
    parts = (ld_spec, st_spec, branch_uop_proxy, ase_spec)
    if all(value is not None for value in parts):
        proxy_sum = sum(value for value in parts if value is not None)
        if proxy_sum > PROXY_SUM_LIMIT:
            return {metric: INVALID for metric in values}
        values["dp_spec"] = max(1 - proxy_sum, 0)
    return values


def stage_metrics(root: Path, stage: str) -> dict[str, MetricValue]:
    timing = read_rows(root, "time", stage)
    topdown = read_rows(root, "topdown", stage)
    branch = read_rows(root, "branch", stage)
    branch_detail = read_rows(root, "branch_detail", stage)
    spec_ls = read_rows(root, "spec_ls", stage)
    spec_ase = read_rows(root, "spec_ase", stage)
    icache = read_rows(root, "icache", stage)
    dcache = read_rows(root, "dcache", stage)
    tlb = read_rows(root, "tlb", stage)
    l3 = read_rows(root, "l3", stage)
    topdown_values = topdown_metrics(topdown)
    spec_values = spec_metrics(spec_ls, spec_ase)
    time_us = mean(timing, lambda row: float(row["wall_time_us"]))
    average_cycles = mean(topdown, lambda row: float(row["cycles"]))

    return {
        "CPU利用率": aggregate_ratio(
            timing,
            ("thread_cpu_time_us",),
            ("wall_time_us",),
        ),
        "频率(MHz)": average_frequency_mhz(average_cycles, time_us),
        "time(us)": time_us,
        "cycles": average_cycles,
        "instructions": mean(topdown, lambda row: float(row["instructions"])),
        "IPC": aggregate_ratio(topdown, ("instructions",), ("cycles",)),
        "Retire": topdown_values["Retire"],
        "FrontendBound": topdown_values["FrontendBound"],
        "Fetch Latency Bound": None,
        "Idle by iTLB Miss": None,
        "Idle by iCache Miss": None,
        "Flush": None,
        "Branch Flush": None,
        "OoO Flush": None,
        "SP Flush": None,
        "Fetch Bandwidth Bound": None,
        "BadSpec": topdown_values["BadSpec"],
        "Branch Mispredicts": None,
        "Indirect Branch": aggregate_ratio(
            branch_detail, ("indirect_branch_misses",), ("branch_misses",)
        ),
        "Push Branch": None,
        "Pop Branch": aggregate_ratio(
            branch_detail, ("return_misses",), ("branch_misses",)
        ),
        "Other Branch": None,
        "Machine Clears": None,
        "Nuke Flush": None,
        "Other Flush": None,
        "BackendBound": topdown_values["BackendBound"],
        "Core Bound": None,
        "Resource Bound": None,
        "FDIV Stall": None,
        "DIV Stall": None,
        "FSU Stall": None,
        "Exe Ports Util": None,
        "Memory Bound": None,
        "L1 Bound": None,
        "L2 Bound": None,
        "L3 Bound": None,
        "Mem Bound": None,
        "Store Bound": None,
        "dp_spec": spec_values["dp_spec"],
        "ld_spec": spec_values["ld_spec"],
        "st_spec": spec_values["st_spec"],
        "branch_spec": aggregate_ratio(branch, ("branches",), ("instructions",)),
        "ase_spec": spec_values["ase_spec"],
        "br missrate": aggregate_ratio(branch, ("branch_misses",), ("branches",)),
        "br mpki": aggregate_ratio(
            branch,
            ("branch_misses",),
            ("instructions",),
            1000,
        ),
        "l1i missrate": aggregate_ratio(
            icache,
            ("l1i_miss_windows",),
            ("l1i_fetch_windows",),
        ),
        "l1i mpki": aggregate_ratio(
            icache,
            ("l1i_miss_windows",),
            ("instructions",),
            1000,
        ),
        "l2i missrate": aggregate_ratio(
            icache,
            ("l2i_misses",),
            ("l2i_accesses",),
        ),
        "l2i mpki": aggregate_ratio(
            icache,
            ("l2i_misses",),
            ("instructions",),
            1000,
        ),
        "l1d missrate": aggregate_ratio(
            dcache,
            ("l1d_miss_proxy",),
            ("ls_ops",),
        ),
        "l1d mpki": aggregate_ratio(
            dcache,
            ("l1d_miss_proxy",),
            ("instructions",),
            1000,
        ),
        "L2d missrate": aggregate_ratio(
            dcache,
            ("l2d_misses",),
            ("l2d_hits", "l2d_misses"),
        ),
        "L2d mpki": aggregate_ratio(
            dcache,
            ("l2d_misses",),
            ("instructions",),
            1000,
        ),
        "L3 missrate": aggregate_ratio(l3, ("l3_misses",), ("l3_accesses",)),
        "L3 mpki": cross_group_ratio(
            l3,
            ("l3_misses",),
            topdown,
            ("instructions",),
            1000,
        ),
        "itlb missrate": UNSUPPORTED,
        "itlb mpki": UNSUPPORTED,
        "dtlb missrate": aggregate_ratio(
            tlb,
            ("l1_dtlb_misses",),
            ("ls_ops",),
        ),
        "dtlb mpki": aggregate_ratio(
            tlb,
            ("l1_dtlb_misses",),
            ("instructions",),
            1000,
        ),
        "stlb missrate": aggregate_ratio(
            tlb,
            ("stlb_misses",),
            ("stlb_hits", "stlb_misses"),
        ),
        "stlb mpki": aggregate_ratio(
            tlb,
            ("stlb_misses",),
            ("instructions",),
            1000,
        ),
    }


def diagnostic_metrics(root: Path, stage: str) -> dict[str, float | None]:
    branch = read_rows(root, "branch_detail", stage)
    frontend = read_rows(root, "frontend_detail", stage)
    backend = read_rows(root, "backend_detail", stage)
    memory = read_rows(root, "memory_detail", stage)
    return {
        "Return missrate": aggregate_ratio(branch, ("return_misses",), ("returns",)),
        "IC DQ empty": aggregate_ratio(frontend, ("ic_dq_empty",), ("cycles",)),
        "IC backpressure": aggregate_ratio(frontend, ("ic_backpressure",), ("cycles",)),
        "ALU token stall": aggregate_ratio(backend, ("alu_token_stall",), ("cycles",)),
        "ALSQ token stall": aggregate_ratio(
            backend, ("alsq_token_stall",), ("cycles",)
        ),
        "Retire token stall": aggregate_ratio(
            backend, ("retire_token_stall",), ("cycles",)
        ),
        "DIV busy": aggregate_ratio(backend, ("div_busy",), ("cycles",)),
        "L2 fill pending": aggregate_ratio(memory, ("l2_fill_pending",), ("cycles",)),
        "iTLB MPKI（候选）": aggregate_ratio(
            memory, ("itlb_l2_hit", "itlb_l2_miss"), ("instructions",), 1000
        ),
    }


def write_diagnostics(root: Path) -> None:
    values = {stage: diagnostic_metrics(root, stage) for stage in STAGES}
    with (root / "zen1_diagnostics.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["指标（Zen1候选，7490未验证）", *STAGES, "口径"])
        for metric, note in DIAGNOSTIC_NOTES.items():
            writer.writerow(
                [
                    metric,
                    *(format_value(metric, values[stage][metric]) for stage in STAGES),
                    note,
                ]
            )


def format_value(metric: str, value: MetricValue) -> str:
    if value is None:
        return "未采集"
    if isinstance(value, str):
        return value
    if metric in PERCENT_METRICS:
        return f"{value:.2%}"
    return f"{value:.2f}"


def write_quality(root: Path) -> None:
    fields = (
        "stage",
        "expected_selected",
        "raw",
        "selected",
        "valid",
        "invalid",
        "status",
    )
    with (root / "collection_quality.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["group", *fields])
        for group in GROUPS:
            if group in ZEN1_GROUPS and not (root / group).exists():
                continue
            with (root / group / "collection_quality.csv").open(
                newline="", encoding="utf-8-sig"
            ) as source:
                for row in csv.DictReader(source):
                    writer.writerow([group, *(row[field] for field in fields)])


def main() -> int:
    args = parse_args()
    values = {stage: stage_metrics(args.run_root, stage) for stage in STAGES}
    stage_cycles = [values[stage]["cycles"] for stage in STAGES]
    cycle_shares: dict[str, float | None] = dict.fromkeys(STAGES)
    if all(isinstance(value, float) for value in stage_cycles):
        numeric_cycles = [value for value in stage_cycles if isinstance(value, float)]
        total_cycles = sum(numeric_cycles)
        if total_cycles > 0:
            cycle_shares = {
                stage: cycles / total_cycles
                for stage, cycles in zip(STAGES, numeric_cycles, strict=True)
            }
    with (args.run_root / "summary.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["", *STAGES])
        for metric in values[STAGES[0]]:
            writer.writerow(
                [
                    metric,
                    *(format_value(metric, values[stage][metric]) for stage in STAGES),
                ]
            )
            if metric == "cycles":
                writer.writerow(
                    [
                        CYCLES_SHARE_METRIC,
                        *(
                            format_value(CYCLES_SHARE_METRIC, cycle_shares[stage])
                            for stage in STAGES
                        ),
                    ]
                )
        writer.writerow([""])
        writer.writerow(["热点函数占比：", *("见热点函数" for _ in STAGES)])
    write_diagnostics(args.run_root)
    write_quality(args.run_root)
    LOGGER.info("wrote %s", args.run_root / "summary.csv")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
