# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Build the Zen 4 phase summary from each independently collected group."""

import argparse
import csv
import json
import logging
from pathlib import Path

if __package__:
    from . import metrics
else:
    import metrics

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


def format_value(metric: str, value: float | None) -> str:
    if value is None:
        return "未采集"
    if metric == "cycle占比" or metric in metrics.PERCENT_METRICS:
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
    profile = (root / "collection_profile").read_text().strip()
    config = json.loads((root / "report_config.json").read_text(encoding="utf-8"))
    groups = ("time", *(group["name"] for group in config["groups"]))
    with (root / "collection_quality.csv").open(
        "w", newline="", encoding="utf-8-sig"
    ) as output:
        writer = csv.writer(output)
        writer.writerow(["group", *fields])
        for source, prefix in ((root, ""), (root / "end_to_end", "end_to_end/")):
            if not prefix and profile == "end_to_end":
                continue
            for group in groups:
                with (source / group / "collection_quality.csv").open(
                    newline="", encoding="utf-8-sig"
                ) as stream:
                    for row in csv.DictReader(stream):
                        writer.writerow(
                            [f"{prefix}{group}", *(row[field] for field in fields)]
                        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_root", type=Path)
    root = parser.parse_args().run_root
    values = {stage: metrics.stage_metrics(root, stage) for stage in STAGES}
    values[END_TO_END_STAGE] = metrics.stage_metrics(
        root / "end_to_end", END_TO_END_STAGE
    )
    cycles = [values[stage]["cycles"] for stage in STAGES]
    total = sum(value for value in cycles if value is not None)
    shares = (
        [value / total for value in cycles]
        if all(value is not None for value in cycles) and total > 0
        else [None] * len(STAGES)
    )
    with (root / "summary.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["", *REPORT_STAGES])
        for metric in values[STAGES[0]]:
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
                        "cycle占比",
                        "不适用",
                        *(format_value("cycle占比", value) for value in shares),
                    ]
                )
        writer.writerow([])
        writer.writerow(
            [
                "热点函数占比：",
                *(
                    "见热点函数" if (root / "hotspot").exists() else "未采集"
                    for _ in REPORT_STAGES
                ),
            ]
        )
    write_quality(root)
    LOGGER.info("wrote %s", root / "summary.csv")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    main()
