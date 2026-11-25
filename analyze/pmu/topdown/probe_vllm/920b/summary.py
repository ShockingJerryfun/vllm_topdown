# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""vLLM-specific phase layout and report assembly."""

import argparse
import csv
import logging
from pathlib import Path

from topdown.profiles.kunpeng920b import (
    CYCLES_SHARE_METRIC,
    PERCENT_METRICS,
    stage_metrics,
)
from topdown.profiles import zen4
from topdown.runner import load_groups

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
GROUPS = ("time", *load_groups("920b"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--pmu-profile", choices=("920b", "zen4"), default="920b")
    return parser.parse_args()


def format_value(metric: str, value: float | None, profile: str = "920b") -> str:
    if value is None:
        return "N/A" if profile == "zen4" else "未采集"
    percentages = zen4.PERCENT_METRICS if profile == "zen4" else PERCENT_METRICS
    if metric == CYCLES_SHARE_METRIC or metric in percentages:
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


def write_quality(root: Path, profile: str = "920b") -> None:
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
            groups = GROUPS if profile == "920b" else ("time", *load_groups(profile))
            for group in groups:
                with (source_root / group / "collection_quality.csv").open(
                    newline="", encoding="utf-8-sig"
                ) as source:
                    for row in csv.DictReader(source):
                        writer.writerow(
                            [f"{prefix}{group}", *(row[field] for field in fields)]
                        )


def main() -> int:
    args = parse_args()
    calculate = zen4.stage_metrics if args.pmu_profile == "zen4" else stage_metrics
    values = {stage: calculate(args.run_root, stage) for stage in STAGES}
    values[END_TO_END_STAGE] = calculate(
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
                        format_value(metric, values[stage][metric], args.pmu_profile)
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
                            format_value(CYCLES_SHARE_METRIC, cycle_shares[stage], args.pmu_profile)
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
    write_quality(args.run_root, args.pmu_profile)
    LOGGER.info("wrote %s", output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
