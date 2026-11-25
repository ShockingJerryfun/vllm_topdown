"""Linux Zen 4 core PMU metrics for independently measured regions.

Formula source: Linux perf amdzen4 pipeline.json and recommended.json at
6628e7d4c6b25c8d18f1345e3a20d21829aae7e4. All event values are thread-scoped.
"""

from __future__ import annotations

import csv
from pathlib import Path

PERCENT_METRICS = {
    "CPU利用率", "Retire", "Retiring Fastpath", "Retiring Microcode",
    "FrontendBound", "Fetch Latency Bound", "Fetch Bandwidth Bound", "BadSpec",
    "BadSpec Mispredict (estimate)", "BadSpec Restart (estimate)", "BackendBound",
    "Backend CPU (estimate)", "Backend Memory (estimate)", "SMTContention",
    "L1 Bound", "L2 Bound", "L3或DRAM等待", "Store Bound", "Resource Bound",
    "Exe Ports Util", "br missrate", "L2 I+D+prefetch missrate", "L2d missrate",
    "L3 missrate",
}

SUMMARY_LEVELS = {
    "Retire": 1, "Retiring Fastpath": 2, "Retiring Microcode": 2,
    "FrontendBound": 1, "Fetch Latency Bound": 2, "Fetch Bandwidth Bound": 2,
    "BadSpec": 1, "BadSpec Mispredict (estimate)": 2,
    "BadSpec Restart (estimate)": 2, "BackendBound": 1,
    "Backend CPU (estimate)": 2, "Backend Memory (estimate)": 2,
    "Resource Bound": 3, "Exe Ports Util": 3, "L1 Bound": 3, "L2 Bound": 3,
    "L3或DRAM等待": 3, "Store Bound": 3, "SMTContention": 1,
}

UNAVAILABLE = {
    "Resource Bound", "Exe Ports Util", "L1 Bound", "L2 Bound", "L3或DRAM等待",
    "Store Bound", "L2d missrate", "L3 missrate",
}

FREQUENCY_LABEL = "PMU窗口频率估算(MHz)"
FREQUENCY_NOTE = (
    "同一 retiring 组各有效样本的 0x76 周期数之和 / time_running 纳秒之和 × 1000；"
    "窗口包括部分打桩开销；周期计数包括用户态和内核态，排除 hypervisor，"
    "但 time_running 不按特权级切分。"
    "这是窗口加权估算，不是独立频率实测或纯业务阶段频率；"
    "缺失、无效或多路复用样本时显示 N/A。"
)
REPORT_NOTE = (
    "Zen 4 各 PMU 组分别采集；各比值使用同组计数。前端和后端父子项来自"
    "不同轮次，可能不闭合；不要强制归一化。SMTContention 单列。"
    "频率为同组 PMU 窗口估算，含部分打桩开销；L2 口径包括 I/D 请求和指定硬件预取。"
)


def read_rows(
    root: Path, group: str, stage: str, *, include_invalid: bool = False
) -> list[dict[str, str]]:
    path = root / group / "parsed" / f"{stage}.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return [
            row for row in csv.DictReader(stream)
            if include_invalid or row.get("valid") == "1"
        ]


def count(rows: list[dict[str, str]], *fields: str) -> float | None:
    if not rows or any(not row.get(field) for row in rows for field in fields):
        return None
    return sum(float(row[field]) for row in rows for field in fields)


def divide(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def scale(value: float | None, factor: float) -> float | None:
    return None if value is None else value * factor


def subtract(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else left - right


def average(rows: list[dict[str, str]], *fields: str) -> float | None:
    return divide(count(rows, *fields), float(len(rows)))


def slots_ratio(rows: list[dict[str, str]], *fields: str) -> float | None:
    return divide(count(rows, *fields), scale(count(rows, "0x76"), 6))


def frequency_mhz(rows: list[dict[str, str]]) -> float | None:
    """Estimate the average over all selected, non-multiplexed PMU windows."""
    if not rows:
        return None
    total_cycles = 0
    total_running_ns = 0
    for row in rows:
        if row.get("valid") != "1":
            return None
        try:
            cycles = int(row["0x76"])
            enabled_ns = int(row["time_enabled"])
            running_ns = int(row["time_running"])
        except (KeyError, TypeError, ValueError):
            return None
        if cycles < 0 or running_ns <= 0 or enabled_ns != running_ns:
            return None
        total_cycles += cycles
        total_running_ns += running_ns
    return total_cycles / total_running_ns * 1000


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    timing = read_rows(root, "time", stage)
    slots = read_rows(root, "slots", stage)
    retiring_rows = read_rows(root, "retiring", stage, include_invalid=True)
    retiring = [row for row in retiring_rows if row.get("valid") == "1"]
    frontend = read_rows(root, "frontend", stage)
    backend = read_rows(root, "backend", stage)
    speculation = read_rows(root, "speculation", stage)
    l2 = read_rows(root, "l2_ratio", stage)
    l2_mpki = read_rows(root, "l2_mpki", stage)
    branch = read_rows(root, "branch", stage)

    cycles = average(retiring, "0x76")
    wall_us = average(timing, "wall_time_us")
    frontend_total = count(frontend, "0x1000001a0")
    frontend_latency = scale(count(frontend, "0x1060001a0"), 6)
    frontend_slots = scale(count(frontend, "0x76"), 6)
    backend_bound = slots_ratio(backend, "0x100001ea0")
    memory_share = divide(count(backend, "0xa2d6"), count(backend, "0x2d6"))
    backend_memory = (
        backend_bound * memory_share
        if backend_bound is not None and memory_share is not None else None
    )
    backend_cpu = (
        backend_bound * (1 - memory_share)
        if backend_bound is not None and memory_share is not None else None
    )
    bad_spec = divide(
        subtract(count(speculation, "0x7aa"), count(speculation, "0xc1")),
        scale(count(speculation, "0x76"), 6),
    )
    mispredict_share = divide(
        count(speculation, "0xc3"), count(speculation, "0xc3", "0x96")
    )
    restart_share = divide(
        count(speculation, "0x96"), count(speculation, "0xc3", "0x96")
    )
    l2_accesses = count(l2, "0xf960", "0xff70", "0xff71", "0xff72")
    l2_misses = count(l2, "0x964", "0xff71", "0xff72")

    return {
        "CPU利用率": divide(count(timing, "thread_cpu_time_us"), count(timing, "wall_time_us")),
        "频率(MHz)": frequency_mhz(retiring_rows),
        "time(us)": wall_us,
        "cycles": cycles,
        "instructions": average(retiring, "0xc0"),
        "IPC": divide(count(retiring, "0xc0"), count(retiring, "0x76")),
        "Retire": slots_ratio(retiring, "0xc1"),
        "Retiring Fastpath": divide(
            subtract(count(retiring, "0xc1"), count(retiring, "0x1000000c2")),
            scale(count(retiring, "0x76"), 6),
        ),
        "Retiring Microcode": slots_ratio(retiring, "0x1000000c2"),
        "FrontendBound": slots_ratio(slots, "0x1000001a0"),
        "Fetch Latency Bound": divide(frontend_latency, frontend_slots),
        "Fetch Bandwidth Bound": divide(subtract(frontend_total, frontend_latency), frontend_slots),
        "BadSpec": bad_spec,
        "BadSpec Mispredict (estimate)": (
            bad_spec * mispredict_share
            if bad_spec is not None and mispredict_share is not None else None
        ),
        "BadSpec Restart (estimate)": (
            bad_spec * restart_share
            if bad_spec is not None and restart_share is not None else None
        ),
        "BackendBound": slots_ratio(slots, "0x100001ea0"),
        "Backend CPU (estimate)": backend_cpu,
        "Resource Bound": None,
        "Exe Ports Util": None,
        "Backend Memory (estimate)": backend_memory,
        "L1 Bound": None,
        "L2 Bound": None,
        "L3或DRAM等待": None,
        "Store Bound": None,
        "SMTContention": slots_ratio(slots, "0x1000060a0"),
        "br missrate": divide(count(branch, "0xc3"), count(branch, "0xc2")),
        "br mpki": scale(divide(count(branch, "0xc3"), count(branch, "0xc0")), 1000),
        "L2 I+D+prefetch access/call": divide(l2_accesses, float(len(l2))),
        "L2 I+D+prefetch miss/call": divide(l2_misses, float(len(l2))),
        "L2 I+D+prefetch missrate": divide(l2_misses, l2_accesses),
        "L2 I+D+prefetch mpki": scale(
            divide(count(l2_mpki, "0x964", "0xff71", "0xff72"), count(l2_mpki, "0xc0")),
            1000,
        ),
        "L2d missrate": None,
        "L3 missrate": None,
    }
