# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Compact report contracts: traceability, event denominators, structured hotspots."""

import csv
import gzip
import json
from collections import Counter
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from scripts import build_xlsx
from scripts.spe import capture, report


def test_details_preserve_source_bytes_without_recursive_copy(tmp_path: Path) -> None:
    source = tmp_path / "topdown/raw/stage.csv"
    source.parent.mkdir(parents=True)
    source.write_text("sequence,cycles\n1,987654321\n")
    build_xlsx.export_details(tmp_path)
    build_xlsx.export_details(tmp_path)
    entries = json.loads((tmp_path / "details/index.json").read_text())
    assert len(entries) == 1
    assert (tmp_path / entries[0]["csv"]).read_bytes() == source.read_bytes()


def test_hotspot_preserves_symbols_and_fraction(tmp_path: Path) -> None:
    source = tmp_path / "perf_report.txt"
    source.write_text("# header\n  60.46%  Worker  lib.so  [.] foo(int, long)\n")
    sheet = Workbook().active
    build_xlsx.write_compact_hotspot(sheet, source, tmp_path)
    assert sheet.cell(2, 1).value == pytest.approx(0.6046)
    assert sheet.cell(2, 4).value == "[.] foo(int, long)"
    with (tmp_path / "details/hotspot.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.reader(stream))
    assert rows[1][3] == "[.] foo(int, long)"


def test_spe_csv_materializes_derived_value_and_preserves_missing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "samples.csv"
    columns = (
        report.Column("difference", "difference", 12),
        report.Column("missing", "missing", 12),
    )
    report.export_csv(path, columns, [[report.Formula("A2-B2", 42), None]])
    with path.open(encoding="utf-8-sig") as stream:
        assert list(csv.reader(stream)) == [["difference", "missing"], ["42", ""]]


def test_spe_percentages_are_fractions_and_identity_stays_in_csv() -> None:
    pc = {
        "samples": 2,
        "event_present": 1,
        "event_bits": {"2": 1},
        "counters": {key: {"present": 0, "missing": 2} for key, _ in report.COUNTERS},
        "elf_pc": "0x1234",
        "pcs": ["0xabcd"],
        "pc_key": "binary-sha:0x1234",
    }
    extra = report.SampleSummary(samples=2)
    values = dict(
        zip(report.SUMMARY_KEYS, report.summary_values(pc, 10, extra), strict=True)
    )
    assert values["sample_share"] == 0.2
    assert values["event_2_rate"] == 1
    assert report.SUMMARY_KEYS[:3] == ("library", "elf_pc", "pcs")
    assert "pc_key" not in report.SUMMARY_KEYS
    assert "pc_key" in {column.key for column in report.PC_COLUMNS}
    pc["event_present"] = 0
    values = dict(
        zip(report.SUMMARY_KEYS, report.summary_values(pc, 10, extra), strict=True)
    )
    assert values["event_2_rate"] is None


def test_csv_export_excludes_internal_interruption_history(tmp_path: Path) -> None:
    live = tmp_path / "topdown/parsed/stage.csv"
    history = tmp_path / ".history/topdown/1/parsed/stage.csv"
    for path in (live, history):
        path.parent.mkdir(parents=True)
        path.write_text("cycles\n100\n")
    build_xlsx.export_details(tmp_path)
    entries = json.loads((tmp_path / "details/index.json").read_text())
    assert [row["source"] for row in entries] == ["topdown/parsed/stage.csv"]


def test_spe_summary_uses_paired_samples_and_exact_addresses(tmp_path: Path) -> None:
    analysis = tmp_path / "spe/analysis"
    analysis.mkdir(parents=True)
    samples: list[dict] = []
    for counters, source, pa in (
        ({"0": 100, "1": 90}, "0x12", "0xff001234567891"),
        ({"0": 20, "1": 10}, "0x10", "0xff001234567891"),
        ({"0": 1000}, None, None),
        ({"1": 50}, None, "0x0"),
    ):
        samples.append(
            {
                "record_id": str(len(samples)),
                "pc_key": "lib:0x100",
                "cpu": 14,
                "op_class": 1,
                "op_value": "0x0",
                "pa": pa,
                "data_source": source,
                "counters": counters,
                "counter_presence": {str(i): str(i) in counters for i in range(32)},
                "tot": counters.get("0"),
                "issue": counters.get("1"),
                "xlat": None,
                "counter6": None,
            }
        )
    with gzip.open(analysis / "samples.jsonl.gz", "wt") as stream:
        for sample in samples:
            stream.write(json.dumps(sample) + "\n")
    (analysis.parent / "spe_capture.json").write_text(
        json.dumps({"cpu_midrs": {"14": "0x480fd060"}})
    )
    pc: dict = {
        "pc_key": "lib:0x100",
        "samples": 4,
        "event_bits": {},
        "elf_pc": "0x100",
        "pcs": ["0x7f000100"],
        "counters": {},
        "binary": "/lib/libcuda.so",
        "instruction": "ldr x0, [x1]",
    }
    for key, _ in report.COUNTERS:
        values = [s["counters"][key] for s in samples if key in s["counters"]]
        pc["counters"][key] = {
            "present": len(values),
            "missing": 4 - len(values),
            "mean": sum(values) / len(values) if values else None,
        }
    (analysis / "pc_summary.json").write_text(json.dumps([pc]))
    for name in ("audit.json", "manifest.json"):
        (analysis / name).write_text(
            json.dumps(
                {
                    "status": "pass",
                    "selected_samples": 4,
                    "pc_count": 1,
                }
            )
        )
    path = tmp_path / "Workload.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Topdown"
    sheet.append(["函数", "测试"])
    sheet.append(["run_fullgraph", "=1+2"])
    for cell in sheet[1]:
        build_xlsx.style_detail_header(cell)
    for row_index in (2, 4):
        for cell in sheet[row_index]:
            build_xlsx.style_detail_body(cell)
    book.save(path)
    book.close()
    report.append_report(path, analysis)
    book = load_workbook(path)
    sheet = book["SPE"]
    row = dict(
        zip(
            report.SUMMARY_KEYS,
            next(sheet.iter_rows(min_row=2, values_only=True)),
            strict=True,
        )
    )
    assert row["difference_mean"] == 10  # Not mean(TOT) - mean(ISSUE).
    assert "0xff001234567891 (2)" in row["pa_summary"]
    assert "0x0 (1)" in row["pa_summary"]
    assert "缺失 1/4" in row["pa_summary"]
    assert (
        row["data_source_summary"]
        == "缺包/未知: 50.00%\n0x12 L1: 25.00%\n0x10 L2: 25.00%"
    )
    assert not any("P95" in str(c.value) or "中位数" in str(c.value) for c in sheet[1])
    assert book["Topdown"]["B2"].value == "=1+2"
    book.close()
    with (tmp_path / "details/spe_samples.csv").open(encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    assert [r["difference"] for r in rows] == ["10", "10", "", ""]
    assert rows[0]["pa"] == "0xff001234567891"
    assert rows[0]["data_source"] == "0x12"


@pytest.mark.parametrize(
    ("midrs", "operation", "source", "expected"),
    [
        ({"14": "0x480fd060"}, "0x0", "0x0", "0x0 Peer CPU"),
        ({"14": "0x480fd060"}, "0x0", None, "缺包/未知"),
        ({"14": "0x480fd060"}, "0x1", "0x12", "0x12（非Load/操作未知）"),
        ({"14": "0x480fd010"}, "0x0", "0x12", "0x12（CPU映射未确认）"),
        ({}, "0x0", "0x12", "0x12（CPU映射未确认）"),
        ({"14": "0x480fd060"}, "0x0", "0xff", "0xff 未知编码"),
    ],
)
def test_spe_source_does_not_invent_cache_hits(midrs, operation, source, expected):
    assert (
        report.source_label(
            {
                "cpu": 14,
                "op_class": 1,
                "op_value": operation,
                "data_source": source,
            },
            midrs,
        )
        == expected
    )


def test_spe_missing_pairs_and_compact_addresses() -> None:
    pc = {
        "samples": 5,
        "event_bits": {},
        "counters": {k: {"present": 0, "missing": 5} for k, _ in report.COUNTERS},
    }
    extra = report.SampleSummary(
        samples=5,
        addresses=Counter({hex(i): 1 for i in range(5)}),
        sources=Counter({"缺包/未知": 5}),
    )
    row = dict(
        zip(report.SUMMARY_KEYS, report.summary_values(pc, 5, extra), strict=True)
    )
    assert row["difference_mean"] is None
    assert row["pa_summary"].count("(1)") == 3
    assert "共 5 个地址" in row["pa_summary"]


def test_capture_retains_midr_for_selected_cpus(tmp_path: Path) -> None:
    path = tmp_path / "cpu14/regs/identification/midr_el1"
    path.parent.mkdir(parents=True)
    path.write_text("0x00000000480fd060\n")
    assert capture.cpu_midrs({14, 16}, tmp_path) == {"14": "0x480fd060"}
