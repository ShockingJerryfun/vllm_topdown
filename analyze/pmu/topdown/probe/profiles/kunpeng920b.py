# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Kunpeng 920B metric calculations for arbitrary named regions."""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path

CYCLES_SHARE_METRIC = "cycle占比"
PERCENT_METRICS = {
    "L2 LSU 命中率", "L2 LSU 推算未命中率",
    "L2 IFU 命中率", "L2 IFU 推算未命中率",
    "L2 MMU 命中率", "L2 MMU 推算未命中率",
    '0 Ports Serialize',
    '0 Ports Non Serialize',
    '1 Ports',
    '2 Ports',
    '3 Ports',
    '4 Ports',
    '5 Ports',
    '6P Ports',
    'L1 DTLB',
    'L1 Misalign',
    'L1 Resource full',
    'L1 Instruction type',
    'L1 Forward hazard',
    'L1 Structure hazard',
    'L1 Pipeline',

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
    "L3或DRAM等待",
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



def read_rows(root: Path, group: str, stage: str) -> list[dict[str, str]]:
    path = root / group / "parsed" / f"{stage}.csv"
    if not path.is_file():
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


def latency_proxy(
    rows: list[dict[str, str]],
    accesses: str,
    refills: str,
    cycles: str,
) -> float | None:
    cycle_sum = total(rows, (cycles,))
    if cycle_sum <= 0:
        return None
    access_sum = total(rows, (accesses,))
    refill_sum = total(rows, (refills,))
    return ((access_sum - refill_sum) * 15 + refill_sum * 100) / cycle_sum


def l2_source_metrics(
    lsu_ifu: list[dict[str, str]], mmu_device: list[dict[str, str]]
) -> dict[str, float | None]:
    metrics = {}
    for source, rows, access, hit in (
        ("LSU", lsu_ifu, "0x6008", "0x600b"),
        ("IFU", lsu_ifu, "0x6009", "0x600c"),
        ("MMU", mmu_device, "0x600a", "0x600d"),
    ):
        hit_rate = ratio(rows, (hit,), (access,))
        metrics[f"L2 {source} 访问（r{access[2:]}，次/调用）"] = (
            total(rows, (access,)) / len(rows) if rows else None
        )
        metrics[f"L2 {source} 命中（r{hit[2:]}，次/调用）"] = (
            total(rows, (hit,)) / len(rows) if rows else None
        )
        metrics[f"L2 {source} 命中率"] = hit_rate
        metrics[f"L2 {source} 推算未命中率"] = residual(1.0, (hit_rate,))
    metrics["LSU 设备 Store（r6010，次/调用）"] = mean(
        mmu_device, lambda row: float(row["0x6010"])
    )
    metrics["LSU NC Write（r6011，次/调用）"] = mean(
        mmu_device, lambda row: float(row["0x6011"])
    )
    return metrics


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    timing = read_rows(root, "time", stage)
    topdown = read_rows(root, "topdown", stage)
    flush = read_rows(root, "flush", stage)
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
    cache = read_rows(root, "cache", stage)
    trans = read_rows(root, "trans", stage)
    levels = read_rows(root, "levels", stage)
    l2_lsu_ifu = read_rows(root, "l2_lsu_ifu", stage)
    l2_mmu_device = read_rows(root, "l2_mmu_device", stage)

    retire = ratio(topdown, ("0x0008",), ("0x0011",), 1 / 6)
    frontend = ratio(topdown, ("0x2011",), ("0x0011",), 1 / 6)
    fetch_latency = ratio(topdown, ("0x2012",), ("0x0011",))
    fetch_bandwidth = None
    if frontend is not None and fetch_latency is not None:
        fetch_bandwidth = frontend - fetch_latency
    branch_flush = ratio(flush, ("0x0010",), ("0x0011",), 5)
    ooo_flush = ratio(flush, ("0x2010",), ("0x0011",), 9)
    sp_flush = ratio(flush, ("0x104f",), ("0x0011",), 5)
    cycles = total(topdown, ("0x0011",))
    bad_spec = None
    if cycles > 0:
        bad_spec = (total(topdown, ("0x001b",)) - total(topdown, ("0x0008",))) / (
            6 * cycles
        )
    topdown_parts = (retire, frontend, bad_spec)
    backend = None
    if all(value is not None for value in topdown_parts):
        backend = 1 - sum(value for value in topdown_parts if value is not None)
    branch_mispredicts = multiply(
        bad_spec,
        ratio(flush, ("0x0010",), ("0x0010", "0x2010")),
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
    nuke_flush = ratio(flush, ("0x200f",), ("0x2010",))
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
    fdiv_stall = ratio(backend_core, ("0x7002",), ("0x0011",))
    div_stall = ratio(backend_core, ("0x7003",), ("0x0011",))
    fsu_stall = ratio(backend_core, ("0x7004",), ("0x0011",))
    exe_ports_util = residual(
        core_bound,
        (fdiv_stall, div_stall, fsu_stall),
    )
    l1_bound = residual(
        ratio(backend_memory, ("0x7005",), ("0x0011",)),
        (ratio(backend_memory, ("0x7007",), ("0x0011",)),),
    )
    l2_bound = residual(
        ratio(backend_memory, ("0x7007",), ("0x0011",)),
        (ratio(backend_memory, ("0x7008",), ("0x0011",)),),
    )
    l3_or_dram = ratio(backend_memory, ("0x7008",), ("0x0011",))
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
        "Idle by iTLB Miss": latency_proxy(
            tlb2,
            "0x0030",
            "0x002e",
            "0x0011",
        ),
        "Idle by iCache Miss": latency_proxy(
            icache,
            "0x0027",
            "0x0028",
            "0x0011",
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
        "Resource Bound": None,
        "FDIV Stall": fdiv_stall,
        "DIV Stall": div_stall,
        "FSU Stall": fsu_stall,
        "Exe Ports Util": exe_ports_util,
        '0 Ports Serialize': ratio(read_rows(root, 'ports_a', stage), ('0x700a',), ("0x0011",)),
        '0 Ports Non Serialize': ratio(read_rows(root, 'ports_a', stage), ('0x700b',), ("0x0011",)),
        '1 Ports': ratio(read_rows(root, 'ports_a', stage), ('0x700c',), ("0x0011",)),
        '2 Ports': ratio(read_rows(root, 'ports_a', stage), ('0x700d',), ("0x0011",)),
        '3 Ports': ratio(read_rows(root, 'ports_b', stage), ('0x700e',), ("0x0011",)),
        '4 Ports': ratio(read_rows(root, 'ports_b', stage), ('0x700f',), ("0x0011",)),
        '5 Ports': ratio(read_rows(root, 'ports_b', stage), ('0x7010',), ("0x0011",)),
        '6P Ports': ratio(read_rows(root, 'ports_b', stage), ('0x7011',), ("0x0011",)),

        "Memory Bound": memory_bound,
        "L1 Bound": l1_bound,
        'L1 DTLB': ratio(read_rows(root, 'l1_detail_a', stage), ('0x5090',), ("0x0011",)),
        'L1 Misalign': ratio(read_rows(root, 'l1_detail_a', stage), ('0x5091',), ("0x0011",)),
        'L1 Resource full': ratio(read_rows(root, 'l1_detail_a', stage), ('0x5092',), ("0x0011",)),
        'L1 Instruction type': ratio(read_rows(root, 'l1_detail_a', stage), ('0x5093',), ("0x0011",)),
        'L1 Forward hazard': ratio(read_rows(root, 'l1_detail_b', stage), ('0x5094',), ("0x0011",)),
        'L1 Structure hazard': ratio(read_rows(root, 'l1_detail_b', stage), ('0x5095',), ("0x0011",)),
        'L1 Pipeline': ratio(read_rows(root, 'l1_detail_b', stage), ('0x5096',), ("0x0011",)),

        "Mem Access（计数/调用）": mean(cache, lambda row: float(row["0x0013"])),
        "L1D Writeback（计数/调用）": mean(trans, lambda row: float(row["0x0015"])),
        "L1D LMiss Read（计数/调用）": mean(cache, lambda row: float(row["0x0039"])),
        "DTLB Walk（计数/调用）": mean(trans, lambda row: float(row["0x0034"])),
        "L2 Bound": l2_bound,
        "L3或DRAM等待": l3_or_dram,
        "LLC Read（计数/调用）": mean(levels, lambda row: float(row["0x0036"])),
        "LLC Read Miss（计数/调用）": mean(levels, lambda row: float(row["0x0037"])),
        "Store Bound": ratio(backend_memory, ("0x7006",), ("0x0011",)),
        "Bus Access（计数/调用）": mean(trans, lambda row: float(row["0x0019"])),
        "Remote Access Read（计数/调用）": mean(trans, lambda row: float(row["0x0038"])),
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
        "L2d missrate": ratio(levels, ("0x0017",), ("0x0016",)),
        "L2d mpki": ratio(levels, ("0x0017",), ("0x0008",), 1000),
        **l2_source_metrics(l2_lsu_ifu, l2_mmu_device),
        "L3 missrate": ratio(l3, ("0x002a",), ("0x002b",)),
        "L3 mpki": ratio(l3, ("0x002a",), ("0x0008",), 1000),
        "itlb missrate": ratio(tlb1, ("0x0002",), ("0x0026",)),
        "itlb mpki": ratio(tlb1, ("0x0002",), ("0x0008",), 1000),
        "dtlb missrate": ratio(tlb1, ("0x0005",), ("0x0025",)),
        "dtlb mpki": ratio(tlb1, ("0x0005",), ("0x0008",), 1000),
        "stlb missrate": ratio(tlb2, ("0x002d", "0x002e"), ("0x002f", "0x0030")),
        "stlb mpki": ratio(tlb2, ("0x002d", "0x002e"), ("0x0008",), 1000),
    }
