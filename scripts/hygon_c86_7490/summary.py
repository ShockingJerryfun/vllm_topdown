#!/usr/bin/env python3

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
GROUPS = ("base", "uops_ls", "frontend", "backend", "dcache", "dtlb")
CYCLES_SHARE_METRIC = "cycles占八阶段总cycles比例"
PERCENT_METRICS = {
    CYCLES_SHARE_METRIC,
    "Branch miss ratio",
    "Frontend starvation ratio",
    "Downstream backpressure",
    "Retire resource pressure",
    "Address/LS pressure",
    "ALU resource pressure",
    "L2 hit ratio",
    "L2 miss ratio",
    "L2 TLB hit ratio",
    "L2 TLB miss ratio",
    "DTLB coverage",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    return parser.parse_args()


def read_rows(root: Path, group: str, stage: str) -> list[dict[str, str]]:
    path = root / group / "parsed" / f"{stage}.csv"
    with path.open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row["valid"] == "1"]


def mean(
    rows: list[dict[str, str]], value: Callable[[dict[str, str]], float]
) -> float | None:
    values = [value(row) for row in rows]
    return sum(values) / len(values) if values else None


def ratio(
    rows: list[dict[str, str]],
    numerator: tuple[str, ...],
    denominator: tuple[str, ...],
    scale: float = 1.0,
) -> float | None:
    valid = [row for row in rows if sum(float(row[name]) for name in denominator) > 0]
    return mean(
        valid,
        lambda row: (
            scale
            * sum(float(row[name]) for name in numerator)
            / sum(float(row[name]) for name in denominator)
        ),
    )


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    base = read_rows(root, "base", stage)
    uops = read_rows(root, "uops_ls", stage)
    frontend = read_rows(root, "frontend", stage)
    backend = read_rows(root, "backend", stage)
    dcache = read_rows(root, "dcache", stage)
    dtlb = read_rows(root, "dtlb", stage)
    return {
        "time(us)": mean(base, lambda row: float(row["duration_us"])),
        "cycles": mean(base, lambda row: float(row["cycles"])),
        "instructions": mean(base, lambda row: float(row["instructions"])),
        "IPC": ratio(base, ("instructions",), ("cycles",)),
        "CPI": ratio(base, ("cycles",), ("instructions",)),
        "Branch MPKI": ratio(base, ("branch_misses",), ("instructions",), 1000),
        "Branch miss ratio": ratio(base, ("branch_misses",), ("branches",)),
        "Branches / inst": ratio(base, ("branches",), ("instructions",)),
        "Retired uops / inst": ratio(base, ("retired_uops",), ("instructions",)),
        "Retired uops / cycle": ratio(base, ("retired_uops",), ("cycles",)),
        "Dispatched uops / inst": ratio(uops, ("dispatched_uops",), ("instructions",)),
        "Dispatched uops / cycle": ratio(uops, ("dispatched_uops",), ("cycles",)),
        "LS ops / inst": ratio(uops, ("ls_ops_dispatched",), ("instructions",)),
        "Frontend starvation ratio": ratio(frontend, ("ic_dq_empty",), ("cycles",)),
        "Downstream backpressure": ratio(frontend, ("ic_backpressure",), ("cycles",)),
        "L1I 32B fetch-window miss MPKI": ratio(
            frontend, ("l1i_fetch_misses",), ("instructions",), 1000
        ),
        "Retire resource pressure": ratio(
            backend, ("retire_token_stalls",), ("cycles",)
        ),
        "Address/LS pressure": ratio(backend, ("agsq_token_stalls",), ("cycles",)),
        "ALU resource pressure": ratio(backend, ("alu_token_stalls",), ("cycles",)),
        "L1D accesses / inst": ratio(dcache, ("l1d_accesses",), ("instructions",)),
        "L2 request activity MPKI": ratio(
            dcache, ("l2_request_activity",), ("instructions",), 1000
        ),
        "L2 demand access MPKI": ratio(
            dcache,
            ("l2_demand_hits", "l2_demand_misses"),
            ("instructions",),
            1000,
        ),
        "L2 miss MPKI": ratio(dcache, ("l2_demand_misses",), ("instructions",), 1000),
        "L2 hit ratio": ratio(
            dcache,
            ("l2_demand_hits",),
            ("l2_demand_hits", "l2_demand_misses"),
        ),
        "L2 miss ratio": ratio(
            dcache,
            ("l2_demand_misses",),
            ("l2_demand_hits", "l2_demand_misses"),
        ),
        "DTLB MPKI": ratio(dtlb, ("l1_dtlb_misses",), ("instructions",), 1000),
        "L2 TLB hit ratio": ratio(
            dtlb, ("dtlb_l2_hits",), ("dtlb_l2_hits", "dtlb_l2_misses")
        ),
        "L2 TLB miss ratio": ratio(
            dtlb, ("dtlb_l2_misses",), ("dtlb_l2_hits", "dtlb_l2_misses")
        ),
        "Page-walk MPKI": ratio(dtlb, ("data_page_walks",), ("instructions",), 1000),
        "DTLB coverage": ratio(
            dtlb, ("dtlb_l2_hits", "dtlb_l2_misses"), ("l1_dtlb_misses",)
        ),
    }


def format_value(metric: str, value: float | None) -> str:
    if value is None:
        return "未采集"
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
            with (root / group / "collection_quality.csv").open(
                newline="", encoding="utf-8-sig"
            ) as source:
                for row in csv.DictReader(source):
                    writer.writerow([group, *(row[field] for field in fields)])


def main() -> int:
    args = parse_args()
    values = {stage: stage_metrics(args.run_root, stage) for stage in STAGES}
    stage_cycles = [values[stage]["cycles"] for stage in STAGES]
    available_cycles = [value for value in stage_cycles if value is not None]
    cycle_shares: dict[str, float | None] = dict.fromkeys(STAGES)
    if len(available_cycles) == len(STAGES):
        total_cycles = sum(available_cycles)
        if total_cycles > 0:
            cycle_shares = {
                stage: cycles / total_cycles
                for stage, cycles in zip(STAGES, available_cycles, strict=True)
            }
    with (args.run_root / "summary.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["C86 Pipeline Bottleneck Analysis", *STAGES])
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
        writer.writerow(["L3/DF", *("未采集" for _ in STAGES)])
        writer.writerow(["热点函数", *("见 hotspot/perf_report.txt" for _ in STAGES)])
    write_quality(args.run_root)
    LOGGER.info("wrote %s", args.run_root / "summary.csv")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
