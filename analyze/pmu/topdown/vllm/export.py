"""Export the existing Topdown summary in the InfraPerf column format."""

import csv
import json
from pathlib import Path

from common.schemas.topdown import METRIC_COLUMNS


def export_metrics(root: Path, run_id: str, chip: str) -> None:
    with (root / "summary.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    stages = rows[0][1:]
    metrics = []
    for row in rows[1:]:
        if not row or not row[0] or row[0] == "热点函数占比：":
            continue
        for stage, text in zip(stages, row[1:], strict=False):
            text = text.strip()
            unit = "pct" if text.endswith("%") else ""
            number = text.removesuffix("%")
            if text in ("", "-", "N/A", "未采集", "不适用"):
                value = None
                status = "not_applicable" if text == "不适用" else "not_collected"
            else:
                value = float(number)
                status = "ok"
            if row[0] == "time(us)":
                unit = "us"
            elif row[0] == "频率(MHz)":
                unit = "mhz"
            metrics.append(dict(zip(METRIC_COLUMNS,
                                    (run_id, chip, stage, row[0], value, unit, status))))
    with (root / "metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=METRIC_COLUMNS)
        writer.writeheader()
        writer.writerows(metrics)
    (root / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
