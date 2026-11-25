"""Check vLLM report labels, hierarchy and missing values."""

import csv
from pathlib import Path

from openpyxl import Workbook, load_workbook

from adapters.vllm import kperf_instrument
from adapters.vllm.scripts.build_xlsx import SUMMARY_METRICS, write_summary
from topdown import probe


def test_vllm_shim_uses_the_independent_probe() -> None:
    assert kperf_instrument.kperf_begin is probe.kperf_begin
    assert kperf_instrument.configure is probe.configure


def test_vllm_summary_keeps_hierarchy_and_additional_metrics(tmp_path: Path) -> None:
    with (tmp_path / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["", "execute_model_to_sample_tokens", "run_fullgraph"])
        for metric in SUMMARY_METRICS:
            value = "未采集" if metric == "Resource Bound" else "12.00%"
            writer.writerow([metric, value, value])

    workbook = Workbook()
    worksheet = workbook.active
    write_summary(worksheet, tmp_path)
    output = tmp_path / "workbook.xlsx"
    workbook.save(output)
    workbook.close()

    result = load_workbook(output)
    sheet = result.active
    rows = {str(row[0].value).lstrip("- "): row for row in sheet.iter_rows(min_row=2)}
    assert sheet.freeze_panes == "B2"
    assert rows["BackendBound"][0].font.bold
    assert rows["L2 IFU Access (r6009)"][0].alignment.indent == 3
    assert "r6009" in rows["L2 IFU Access (r6009)"][0].comment.text
    assert "1 - hit/access" in rows["L2 IFU Miss Rate (calc)"][0].comment.text
    assert rows["Resource Bound"][1].value == "未采集"
    assert rows["频率(MHz)"][0].comment is not None
    result.close()
