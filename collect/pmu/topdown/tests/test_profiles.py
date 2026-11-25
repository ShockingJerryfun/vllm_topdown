"""Profile formulas and result presentation on a non-vLLM region."""

import csv
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook

from topdown.profiles.kunpeng920b import stage_metrics
from topdown.report import write_workbook
from topdown.runner import load_groups


def write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def test_920b_22_groups_and_vllm_config_agree() -> None:
    groups = load_groups("920b")
    config = Path(__file__).parents[1] / "adapters/vllm/scripts/920b/report_config.json"
    report_groups = json.loads(config.read_text())["groups"]
    assert len(groups) == len(report_groups) == 22
    assert {group["name"]: group["events"] for group in report_groups} == groups
    assert all("0x816c" not in events for events in groups.values())


def test_arbitrary_region_uses_group_denominators_and_preserves_missing(tmp_path: Path) -> None:
    stage = "queue_prepare"
    for name, events in load_groups("920b").items():
        row = {event: 100 for event in events}
        row.update({event: value for event, value in {
            "0x0011": 1000, "0x0008": 500, "0x5090": 10, "0x700b": 200,
            "0x6008": 100, "0x600b": 60, "0x6009": 50, "0x600c": 10,
            "0x600a": 10, "0x600d": 8, "0x6010": 7, "0x6011": 5,
        }.items() if event in row})
        row["valid"] = 1
        write_rows(tmp_path / name / "parsed" / f"{stage}.csv", [row])
    write_rows(tmp_path / "time/parsed" / f"{stage}.csv", [{
        "valid": 1, "wall_time_us": 100, "thread_cpu_time_us": 80,
    }])
    result = stage_metrics(tmp_path, stage)
    assert result["IPC"] == pytest.approx(0.5)
    assert result["L1 DTLB"] == pytest.approx(0.01)
    assert result["0 Ports Non Serialize"] == pytest.approx(0.2)
    assert result["L2 LSU 命中率"] == pytest.approx(0.6)
    assert result["L2 IFU 推算未命中率"] == pytest.approx(0.8)
    assert result["L2 MMU 命中率"] == pytest.approx(0.8)
    assert result["LSU 设备 Store（r6010，次/调用）"] == 7
    assert result["Resource Bound"] is None
    output = write_workbook(tmp_path, {stage: result})
    book = load_workbook(output)
    sheet = book["Topdown"]
    assert sheet.freeze_panes == "B2" and sheet["B1"].value == stage
    labels = {str(row[0].value).lstrip("- "): row[1] for row in sheet.iter_rows(min_row=2)}
    assert labels["Resource Bound"].value == "-"
    assert labels["L2 LSU Hit Rate"].number_format == "0.00%"
    assert sheet.cell(labels["BackendBound"].row, 1).font.bold
    assert sheet.cell(labels["L2 LSU Hit Rate"].row, 1).alignment.indent == 3
    book.close()
