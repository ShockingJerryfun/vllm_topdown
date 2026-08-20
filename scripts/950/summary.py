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
GROUPS = (
    "topdown",
    "icache",
    "dcache",
    "l3",
    "tlb1",
    "tlb2",
    "branch",
    "imix",
    "imix2",
)
PERCENT_METRICS = {
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


def ratio(
    rows: list[dict[str, str]],
    numerator: tuple[str, ...],
    denominator: tuple[str, ...],
    scale: float = 1.0,
) -> float | None:
    valid = [row for row in rows if sum(float(row[code]) for code in denominator) > 0]
    return mean(
        valid,
        lambda row: (
            scale
            * sum(float(row[code]) for code in numerator)
            / sum(float(row[code]) for code in denominator)
        ),
    )


def add(values: tuple[float | None, ...]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def stage_metrics(root: Path, stage: str) -> dict[str, float | None]:
    topdown = [
        row for row in read_rows(root, "topdown", stage) if float(row["0x0011"]) > 0
    ]
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
    bad_spec = mean(
        topdown,
        lambda row: (
            (float(row["0x001b"]) - float(row["0x0008"])) / (8 * float(row["0x0011"]))
        ),
    )
    backend = mean(
        topdown,
        lambda row: (
            1
            - float(row["0x0008"]) / (8 * float(row["0x0011"]))
            - float(row["0x1f21"]) / (8 * float(row["0x0011"]))
            - (float(row["0x001b"]) - float(row["0x0008"])) / (8 * float(row["0x0011"]))
        ),
    )
    branch_spec = add(
        (
            ratio(imix, ("0x0078",), ("0x001b",)),
            ratio(imix2, ("0x0079", "0x007a"), ("0x001b",)),
        )
    )

    return {
        "cycles": mean(topdown, lambda row: float(row["0x0011"])),
        "instructions": mean(topdown, lambda row: float(row["0x0008"])),
        "IPC/Retire": retire,
        "FrontendBound": frontend,
        "BackendBound": backend,
        "BadSpec": bad_spec,
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
    metrics = list(values[STAGES[0]])
    output = args.run_root / "summary.csv"
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(["", *STAGES])
        writer.writerow(["CPU利用率", *("未采集" for _ in STAGES)])
        for metric in metrics:
            writer.writerow(
                [
                    metric,
                    *(format_value(metric, values[stage][metric]) for stage in STAGES),
                ]
            )
        writer.writerow([""])
        writer.writerow(["热点函数占比：", *("见热点函数" for _ in STAGES)])
    write_quality(args.run_root)
    LOGGER.info("wrote %s", output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
