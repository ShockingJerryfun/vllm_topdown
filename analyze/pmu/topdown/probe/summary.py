"""Calculate Topdown metrics and create the existing workbook."""

import csv
import json
from pathlib import Path

from common.schemas.topdown import METRIC_COLUMNS

from .profiles import kunpeng920b, zen4
from .report import write_workbook


def write_summary(
    root: Path, stages: list[str], profile: str = "920b", output: Path | None = None
) -> None:
    if profile not in ("920b", "zen4"):
        raise ValueError(f"Unsupported CPU profile: {profile}")
    output = output or root
    output.mkdir(parents=True, exist_ok=True)
    metrics_module = kunpeng920b if profile == "920b" else zen4
    values = {stage: metrics_module.stage_metrics(root, stage) for stage in stages}
    metrics = list(values[stages[0]])
    with (output / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["指标", *stages])
        for metric in metrics:
            row = [values[stage][metric] for stage in stages]
            writer.writerow([metric, *("N/A" if profile == "zen4" and value is None else value
                                      for value in row)])
    (output / "summary.json").write_text(
        json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    records = []
    for stage, stage_values in values.items():
        for metric, value in stage_values.items():
            unit = "pct" if metric in metrics_module.PERCENT_METRICS else ""
            if metric == "time(us)":
                unit = "us"
            elif metric == "频率(MHz)":
                unit = "mhz"
            number = value * 100 if value is not None and unit == "pct" else value
            status = "ok" if value is not None else "not_collected"
            records.append(dict(zip(METRIC_COLUMNS,
                                    (root.name, profile, stage, metric, number, unit, status))))
    with (output / "metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=METRIC_COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    (output / "metrics.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    write_workbook(output, values, profile)
