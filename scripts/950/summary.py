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
END_TO_END_STAGE = "execute_model_to_sample_tokens"
REPORT_STAGES = (END_TO_END_STAGE, *STAGES)
GROUPS = (
    "time",
    "topdown",
    "frontend_detail",
    "badspec_branch",
    "backend_core",
    "backend_memory",
    "icache",
    "dcache",
    "l3",
    "tlb1",
    "tlb2",
    "branch",
    "imix",
    "imix2",
)
CYCLES_SHARE_METRIC = "cycle占比"
PERCENT_METRICS = {
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
    if (root / "collection_profile").is_file() and (
        root / "collection_profile"
    ).read_text().strip() == "end_to_end":
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return [
            {key: value or "" for key, value in row.items() if key is not None}
            for row in csv.DictReader(handle)
            if row["valid"] == "1"
        ]


def mean(
    rows: list[dict[str, str]], value: Callable[[dict[str, str]], float]
) -> float | None:
    values = [value(row) for row in rows]
    return sum(values) / len(values) if values else None


def total(rows: list[dict[str, str]], fields: tuple[str, ...]) -> float:
    return sum(sum(float(row[field]) for field in fields) for row in rows)


def ratio(
    rows: list[dict[str, str]],
    numerator: tuple[str, ...],
    denominator: tuple[str, ...],
    scale: float = 1.0,
) -> float | None:
    denominator_sum = total(rows, denominator)
    if denominator_sum <= 0:
        return None
    return scale * total(rows, numerator) / denominator_sum


def add(values: tuple[float | None, ...]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def multiply(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left * right


def residual(
    parent: float | None,
    children: tuple[float | None, ...],
) -> float | None:
    if parent is None or any(child is None for child in children):
        return None
    return parent - sum(child for child in children if child is not None)


def residual_ratio(
    rows: list[dict[str, str]],
    total_field: str,
    subtract_fields: tuple[str, ...],
) -> float | None:
    denominator = total(rows, (total_field,))
    if denominator <= 0:
        return None
    return (denominator - total(rows, subtract_fields)) / denominator


def average_frequency_mhz(
    cycles: float | None,
    time_us: float | None,
) -> float | None:
    if cycles is None or time_us is None or time_us <= 0:
        return None
    return cycles / time_us


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    timing = read_rows(root, "time", stage)
    topdown = [
        row for row in read_rows(root, "topdown", stage) if float(row["0x0011"]) > 0
    ]
    frontend_detail = read_rows(root, "frontend_detail", stage)
    badspec_branch = read_rows(root, "badspec_branch", stage)
    backend_core = read_rows(root, "backend_core", stage)
    backend_memory = read_rows(root, "backend_memory", stage)
    imix = read_rows(root, "imix", stage)
    imix2 = read_rows(root, "imix2", stage)
    branch = read_rows(root, "branch", stage)
    icache = read_rows(root, "icache", stage)
    dcache = read_rows(root, "dcache", stage)
    l3 = read_rows(root, "l3", stage)
    tlb1 = read_rows(root, "tlb1", stage)
    tlb2 = read_rows(root, "tlb2", stage)

    retire = ratio(topdown, ("0x0008",), ("0x0011",), 1 / 8)
    frontend = ratio(topdown, ("0x1f21",), ("0x0011",), 1 / 8)
    fetch_latency = ratio(topdown, ("0x1f22",), ("0x0011",))
    fetch_bandwidth = None
    if frontend is not None and fetch_latency is not None:
        fetch_bandwidth = frontend - fetch_latency
    branch_flush = ratio(frontend_detail, ("0x1f14",), ("0x0011",))
    ooo_flush = ratio(frontend_detail, ("0x1f12",), ("0x0011",))
    sp_flush = ratio(frontend_detail, ("0x1f13",), ("0x0011",))
    cycles = total(topdown, ("0x0011",))
    bad_spec = None
    if cycles > 0:
        bad_spec = (total(topdown, ("0x001b",)) - total(topdown, ("0x0008",))) / (
            8 * cycles
        )
    topdown_parts = (retire, frontend, bad_spec)
    backend = None
    if all(value is not None for value in topdown_parts):
        backend = 1 - sum(value for value in topdown_parts if value is not None)
    branch_mispredicts = multiply(
        bad_spec,
        ratio(
            badspec_branch,
            ("0x0010",),
            ("0x0010", "0x2010"),
        ),
    )
    machine_clears = residual(bad_spec, (branch_mispredicts,))
    indirect_branch = ratio(badspec_branch, ("0x1010",), ("0x0010",))
    push_branch = ratio(
        badspec_branch,
        ("0x1013", "0x1016"),
        ("0x0010",),
    )
    pop_branch = ratio(badspec_branch, ("0x100d",), ("0x0010",))
    other_branch = residual_ratio(
        badspec_branch,
        "0x0010",
        ("0x1013", "0x1016", "0x100d"),
    )
    nuke_flush = ratio(branch, ("0x203f",), ("0x2040",))
    other_flush = residual(1.0, (nuke_flush,))

    memory_bound = ratio(
        backend_memory,
        ("0x7005", "0x7006"),
        ("0x0011",),
    )
    core_bound = residual(
        ratio(backend_core, ("0x7001",), ("0x0011",)),
        (memory_bound,),
    )
    resource_bound = ratio(backend_core, ("0x7000",), ("0x0011",))
    fdiv_stall = ratio(backend_core, ("0x7002",), ("0x0011",))
    div_stall = ratio(backend_core, ("0x7003",), ("0x0011",))
    fsu_stall = ratio(backend_core, ("0x7004",), ("0x0011",))
    exe_ports_util = residual(
        core_bound,
        (resource_bound, fdiv_stall, div_stall, fsu_stall),
    )
    l1_bound = residual(
        ratio(backend_memory, ("0x7005",), ("0x0011",)),
        (ratio(backend_memory, ("0x7007",), ("0x0011",)),),
    )
    l2_bound = residual(
        ratio(backend_memory, ("0x7007",), ("0x0011",)),
        (ratio(backend_memory, ("0x7008",), ("0x0011",)),),
    )
    l3_bound = residual(
        ratio(backend_memory, ("0x7008",), ("0x0011",)),
        (ratio(backend_memory, ("0x7009",), ("0x0011",)),),
    )
    branch_spec = add(
        (
            ratio(imix, ("0x0078",), ("0x001b",)),
            ratio(imix2, ("0x0079", "0x007a"), ("0x001b",)),
        )
    )
    time_us = mean(timing, lambda row: float(row["wall_time_us"]))
    average_cycles = mean(topdown, lambda row: float(row["0x0011"]))

    return {
        "CPU利用率": ratio(
            timing,
            ("thread_cpu_time_us",),
            ("wall_time_us",),
        ),
        "频率(MHz)": average_frequency_mhz(average_cycles, time_us),
        "time(us)": time_us,
        "cycles": average_cycles,
        "instructions": mean(topdown, lambda row: float(row["0x0008"])),
        "IPC": ratio(topdown, ("0x0008",), ("0x0011",)),
        "Retire": retire,
        "FrontendBound": frontend,
        "Fetch Latency Bound": fetch_latency,
        "Idle by iTLB Miss": ratio(
            frontend_detail,
            ("0x1f10",),
            ("0x0011",),
        ),
        "Idle by iCache Miss": ratio(
            frontend_detail,
            ("0x1f11",),
            ("0x0011",),
        ),
        "Flush": add((branch_flush, ooo_flush, sp_flush)),
        "Branch Flush": branch_flush,
        "OoO Flush": ooo_flush,
        "SP Flush": sp_flush,
        "Fetch Bandwidth Bound": fetch_bandwidth,
        "BadSpec": bad_spec,
        "Branch Mispredicts": branch_mispredicts,
        "Indirect Branch": indirect_branch,
        "Push Branch": push_branch,
        "Pop Branch": pop_branch,
        "Other Branch": other_branch,
        "Machine Clears": machine_clears,
        "Nuke Flush": nuke_flush,
        "Other Flush": other_flush,
        "BackendBound": backend,
        "Core Bound": core_bound,
        "Resource Bound": resource_bound,
        "FDIV Stall": fdiv_stall,
        "DIV Stall": div_stall,
        "FSU Stall": fsu_stall,
        "Exe Ports Util": exe_ports_util,
        "Memory Bound": memory_bound,
        "L1 Bound": l1_bound,
        "L2 Bound": l2_bound,
        "L3 Bound": l3_bound,
        "Mem Bound": ratio(backend_memory, ("0x7009",), ("0x0011",)),
        "Store Bound": ratio(backend_memory, ("0x7006",), ("0x0011",)),
        "dp_spec": ratio(imix, ("0x0073",), ("0x001b",)),
        "ld_spec": ratio(imix, ("0x0070",), ("0x001b",)),
        "st_spec": ratio(imix2, ("0x0071",), ("0x001b",)),
        "branch_spec": branch_spec,
        "ase_spec": ratio(imix, ("0x8005",), ("0x001b",)),
        "br missrate": ratio(branch, ("0x0022",), ("0x0021",)),
        "br mpki": ratio(branch, ("0x0022",), ("0x0008",), 1000),
        "l1i missrate": ratio(icache, ("0x0001",), ("0x0014",)),
        "l1i mpki": ratio(icache, ("0x0001",), ("0x0008",), 1000),
        "l2i missrate": ratio(icache, ("0x0028",), ("0x0027",)),
        "l2i mpki": ratio(icache, ("0x0028",), ("0x0008",), 1000),
        "l1d missrate": ratio(dcache, ("0x0003",), ("0x0004",)),
        "l1d mpki": ratio(dcache, ("0x0003",), ("0x0008",), 1000),
        "L2d missrate": ratio(dcache, ("0x0017",), ("0x0016",)),
        "L2d mpki": ratio(dcache, ("0x0017",), ("0x0008",), 1000),
        "L3 missrate": ratio(l3, ("0x002a",), ("0x002b",)),
        "L3 mpki": ratio(l3, ("0x002a",), ("0x0008",), 1000),
        "itlb missrate": ratio(tlb1, ("0x0002",), ("0x0026",)),
        "itlb mpki": ratio(tlb1, ("0x0002",), ("0x0008",), 1000),
        "dtlb missrate": ratio(tlb1, ("0x0005",), ("0x0025",)),
        "dtlb mpki": ratio(tlb1, ("0x0005",), ("0x0008",), 1000),
        "stlb missrate": ratio(tlb2, ("0x002d", "0x002e"), ("0x002f", "0x0030")),
        "stlb mpki": ratio(tlb2, ("0x002d", "0x002e"), ("0x0008",), 1000),
    }


def format_value(metric: str, value: float | None) -> str:
    if value is None:
        return "未采集"
    if metric in PERCENT_METRICS:
        return f"{value:.2%}"
    return f"{value:.2f}"


def pipeline_cycle_shares(
    values: dict[str, dict[str, float | None]],
) -> dict[str, float | None]:
    stage_cycles = [values[stage]["cycles"] for stage in STAGES]
    available_cycles = [value for value in stage_cycles if value is not None]
    shares: dict[str, float | None] = dict.fromkeys(STAGES)
    if len(available_cycles) != len(STAGES):
        return shares
    total_cycles = sum(available_cycles)
    if total_cycles <= 0:
        return shares
    return {
        stage: cycles / total_cycles
        for stage, cycles in zip(STAGES, available_cycles, strict=True)
    }


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
        quality_roots = (
            (root, ""),
            (root / "end_to_end", "end_to_end/"),
        )
        for source_root, prefix in quality_roots:
            if (
                not prefix
                and (root / "collection_profile").is_file()
                and (root / "collection_profile").read_text().strip() == "end_to_end"
            ):
                continue
            for group in GROUPS:
                with (source_root / group / "collection_quality.csv").open(
                    newline="", encoding="utf-8-sig"
                ) as source:
                    for row in csv.DictReader(source):
                        writer.writerow(
                            [f"{prefix}{group}", *(row[field] for field in fields)]
                        )


def main() -> int:
    args = parse_args()
    values = {stage: stage_metrics(args.run_root, stage) for stage in STAGES}
    values[END_TO_END_STAGE] = stage_metrics(
        args.run_root / "end_to_end",
        END_TO_END_STAGE,
    )
    cycle_shares = pipeline_cycle_shares(values)
    metrics = list(values[STAGES[0]])
    output = args.run_root / "summary.csv"
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["", *REPORT_STAGES])
        for metric in metrics:
            writer.writerow(
                [
                    metric,
                    *(
                        format_value(metric, values[stage][metric])
                        for stage in REPORT_STAGES
                    ),
                ]
            )
            if metric == "cycles":
                writer.writerow(
                    [
                        CYCLES_SHARE_METRIC,
                        "不适用",
                        *(
                            format_value(CYCLES_SHARE_METRIC, cycle_shares[stage])
                            for stage in STAGES
                        ),
                    ]
                )
        writer.writerow([""])
        writer.writerow(
            [
                "热点函数占比：",
                *(
                    "见热点函数" if (args.run_root / "hotspot").exists() else "未采集"
                    for _ in REPORT_STAGES
                ),
            ]
        )
    write_quality(args.run_root)
    LOGGER.info("wrote %s", output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
