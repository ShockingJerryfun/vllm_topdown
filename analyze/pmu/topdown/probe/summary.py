"""Calculate Topdown metrics and create the existing workbook."""

import csv
import json
from pathlib import Path

from .profiles import kunpeng920b, zen4
from .report import write_workbook


def write_summary(root: Path, stages: list[str], profile: str = "920b") -> None:
    if profile not in ("920b", "zen4"):
        raise ValueError(f"Unsupported CPU profile: {profile}")
    metrics_module = kunpeng920b if profile == "920b" else zen4
    values = {stage: metrics_module.stage_metrics(root, stage) for stage in stages}
    metrics = list(values[stages[0]])
    with (root / "summary.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["指标", *stages])
        for metric in metrics:
            row = [values[stage][metric] for stage in stages]
            writer.writerow([metric, *("N/A" if profile == "zen4" and value is None else value
                                      for value in row)])
    (root / "summary.json").write_text(
        json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    write_workbook(root, values, profile)
