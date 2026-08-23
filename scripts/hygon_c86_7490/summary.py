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
GROUPS = ("time", "base", "uops_ls", "frontend", "backend", "dcache", "dtlb")
CYCLES_SHARE_METRIC = "cycles占八阶段总cycles比例"
PERCENT_METRICS = {
    "CPU利用率",
    CYCLES_SHARE_METRIC,
    "IPC/Retire",
    "FrontendBound",
    "BackendBound",
    "BadSpec",
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


def aggregate_ratio(
    rows: list[dict[str, str]],
    numerator: str,
    denominator: str,
) -> float | None:
    valid = [row for row in rows if float(row[denominator]) > 0]
    if not valid:
        return None
    return sum(float(row[numerator]) for row in valid) / sum(
        float(row[denominator]) for row in valid
    )


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    timing = read_rows(root, "time", stage)
    base = read_rows(root, "base", stage)
    dcache = read_rows(root, "dcache", stage)
    dtlb = read_rows(root, "dtlb", stage)
    return {
        "CPU利用率": aggregate_ratio(
            timing,
            "thread_cpu_time_us",
            "wall_time_us",
        ),
        "time(us)": mean(timing, lambda row: float(row["wall_time_us"])),
        "cycles": mean(base, lambda row: float(row["cycles"])),
        "instructions": mean(base, lambda row: float(row["instructions"])),
        "IPC/Retire": None,
        "FrontendBound": None,
        "BackendBound": None,
        "BadSpec": None,
        "dp_spec": None,
        "ld_spec": None,
        "st_spec": None,
        "branch_spec": None,
        "ase_spec": None,
        "br missrate": ratio(base, ("branch_misses",), ("branches",)),
        "br mpki": ratio(base, ("branch_misses",), ("instructions",), 1000),
        "l1i missrate": None,
        "l1i mpki": None,
        "l2i missrate": None,
        "l2i mpki": None,
        "l1d missrate": None,
        "l1d mpki": None,
        "L2d missrate": ratio(
            dcache,
            ("l2_demand_misses",),
            ("l2_demand_hits", "l2_demand_misses"),
        ),
        "L2d mpki": ratio(
            dcache,
            ("l2_demand_misses",),
            ("instructions",),
            1000,
        ),
        "L3 missrate": None,
        "L3 mpki": None,
        "itlb missrate": None,
        "itlb mpki": None,
        "dtlb missrate": None,
        "dtlb mpki": ratio(dtlb, ("l1_dtlb_misses",), ("instructions",), 1000),
        "stlb missrate": ratio(
            dtlb, ("dtlb_l2_misses",), ("dtlb_l2_hits", "dtlb_l2_misses")
        ),
        "stlb mpki": ratio(
            dtlb,
            ("dtlb_l2_misses",),
            ("instructions",),
            1000,
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
    write_quality(args.run_root)
    LOGGER.info("wrote %s", args.run_root / "summary.csv")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
