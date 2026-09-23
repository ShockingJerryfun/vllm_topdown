#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import shutil
import subprocess
import sys
from copy import copy
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import regex as re
from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import Cell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

LOGGER = logging.getLogger(__name__)

STAGE_SHEETS = (
    ("add_requests", ("add_requests",)),
    ("prepare_inputs", ("prepare_inputs",)),
    (
        "prepare_attn",
        ("prepare_attn_runner", "prepare_attn_model_state"),
    ),
    ("run_fullgraph", ("run_fullgraph",)),
    ("sample", ("sample",)),
    ("output", ("async_output_init", "postprocess_sampled")),
)
END_TO_END_STAGE = "execute_model_to_sample_tokens"
END_TO_END_SHEET = "execute_to_sample"
BASE_HEADERS = (
    "函数",
    "序号",
    "全局调用",
    "统计范围",
    "时间(us)",
    "time_enabled",
    "time_running",
    "valid",
)
SUMMARY_METRICS = (
    "CPU利用率",
    "频率(MHz)",
    "time(us)",
    "cycles",
    "cycle占比",
    "instructions",
    "IPC",
    "Retire",
    "FrontendBound",
    "Fetch Latency Bound",
    "Idle by iTLB Miss",
    "Idle by iCache Miss",
    "Flush",
    "Branch Flush",
    "OoO Flush",
    "SP Flush",
    "Fetch Bandwidth Bound",
    "BadSpec",
    "Branch Mispredicts",
    "Indirect Branch",
    "Push Branch",
    "Pop Branch",
    "Other Branch",
    "Machine Clears",
    "Nuke Flush",
    "Other Flush",
    "BackendBound",
    "Core Bound",
    "Resource Bound",
    "FDIV Stall",
    "DIV Stall",
    "FSU Stall",
    "Exe Ports Util",
    "Memory Bound",
    "L1 Bound",
    "L2 Bound",
    "L3 Bound",
    "Mem Bound",
    "Store Bound",
    "dp_spec",
    "ld_spec",
    "st_spec",
    "branch_spec",
    "ase_spec",
    "br missrate",
    "br mpki",
    "l1i missrate",
    "l1i mpki",
    "l2i missrate",
    "l2i mpki",
    "l1d missrate",
    "l1d mpki",
    "L2d missrate",
    "L2d mpki",
    "L3 missrate",
    "L3 mpki",
    "itlb missrate",
    "itlb mpki",
    "dtlb missrate",
    "dtlb mpki",
    "stlb missrate",
    "stlb mpki",
)
SUMMARY_LEVELS = {
    "cycle占比": 2,
    "Retire": 1,
    "FrontendBound": 1,
    "Fetch Latency Bound": 2,
    "Idle by iTLB Miss": 3,
    "Idle by iCache Miss": 3,
    "Flush": 3,
    "Branch Flush": 4,
    "OoO Flush": 4,
    "SP Flush": 4,
    "Fetch Bandwidth Bound": 2,
    "BadSpec": 1,
    "Branch Mispredicts": 2,
    "Indirect Branch": 3,
    "Push Branch": 3,
    "Pop Branch": 3,
    "Other Branch": 3,
    "Machine Clears": 2,
    "Nuke Flush": 3,
    "Other Flush": 3,
    "BackendBound": 1,
    "Core Bound": 2,
    "Resource Bound": 3,
    "FDIV Stall": 3,
    "DIV Stall": 3,
    "FSU Stall": 3,
    "Exe Ports Util": 3,
    "Memory Bound": 2,
    "L1 Bound": 3,
    "L2 Bound": 3,
    "L3 Bound": 3,
    "Mem Bound": 3,
    "Store Bound": 3,
}
SUMMARY_GROUP_STARTS = {
    "CPU利用率",
    "Retire",
    "dp_spec",
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
FORMULA_TOKEN = re.compile(r"\{([^{}]+)\}")
SAFE_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
NUMBER = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
BENCHMARK_VALUE = re.compile(
    r"^\s*(?P<label>.+?):\s*"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*$"
)

HEADER_FILL = "FF1F4E78"
HEADER_BORDER_COLOR = "FFB4C6E7"
BODY_BORDER_COLOR = "FFD9E2F3"
SUMMARY_LABEL_FILL = "FFD9EAF7"
SUMMARY_LABEL_FONT = "FF17365D"
SUMMARY_LEVEL_FILLS = {
    1: "FFD9EAF7",
    2: "FFEDF4FA",
    3: "FFF5F9FC",
    4: "FFFAFCFE",
}
WHITE = "FFFFFFFF"
BLACK = "FF000000"

HEADER_BORDER = Border(
    left=Side(style="thin", color=HEADER_BORDER_COLOR),
    right=Side(style="thin", color=HEADER_BORDER_COLOR),
    top=Side(style="thin", color=HEADER_BORDER_COLOR),
    bottom=Side(style="thin", color=HEADER_BORDER_COLOR),
)
BODY_BORDER = Border(
    left=Side(style="thin", color=BODY_BORDER_COLOR),
    right=Side(style="thin", color=BODY_BORDER_COLOR),
    top=Side(style="thin", color=BODY_BORDER_COLOR),
    bottom=Side(style="thin", color=BODY_BORDER_COLOR),
)
SUMMARY_SECTION_BORDER = Border(
    left=Side(style="thin", color=BODY_BORDER_COLOR),
    right=Side(style="thin", color=BODY_BORDER_COLOR),
    top=Side(style="medium", color=HEADER_BORDER_COLOR),
    bottom=Side(style="thin", color=BODY_BORDER_COLOR),
)


@dataclass(frozen=True)
class DerivedMetric:
    name: str
    number_format: str
    formula: str


@dataclass(frozen=True)
class GroupSpec:
    name: str
    suffix: str
    events: tuple[str, ...]
    event_headers: tuple[str, ...]
    semantic_headers: tuple[str, ...]
    derived: tuple[DerivedMetric, ...]


@dataclass(frozen=True)
class StageRow:
    sequence: int
    global_call: int
    wall_time_us: float | None
    time_enabled: int
    time_running: int
    valid: int
    scope: str
    counts: dict[str, int]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the final vLLM PMU Excel workbook."
    )
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--chip", required=True)
    parser.add_argument("--version", default="0.26")
    parser.add_argument("--model-short", default="qwen3")
    parser.add_argument("--input-len", type=int, default=7000)
    parser.add_argument("--output-len", type=int, default=100)
    parser.add_argument("--include-end-to-end", action="store_true")
    parser.add_argument("--compact-report", action="store_true")
    parser.add_argument("--spe-dir", type=Path)
    parser.add_argument("--spe-report", type=Path)
    parser.add_argument("--defer-spe", action="store_true")
    return parser.parse_args()


def require_mapping(value: object, context: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{context} must be an object")
    return value


def require_string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context} must be a non-empty string")
    return value


def require_string_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{context} must be a list of non-empty strings")
    return tuple(value)


def load_config(path: Path) -> tuple[GroupSpec, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    root = require_mapping(payload, str(path))
    groups_value = root.get("groups")
    if not isinstance(groups_value, list) or not groups_value:
        raise ValueError(f"{path}: groups must be a non-empty list")

    groups: list[GroupSpec] = []
    for group_index, group_value in enumerate(groups_value):
        context = f"{path}: groups[{group_index}]"
        group = require_mapping(group_value, context)
        name = require_string(group.get("name"), f"{context}.name")
        suffix = require_string(group.get("suffix"), f"{context}.suffix")
        events = require_string_list(group.get("events"), f"{context}.events")
        event_headers_value = group.get("event_headers", list(events))
        event_headers = require_string_list(
            event_headers_value, f"{context}.event_headers"
        )
        if len(event_headers) != len(events):
            raise ValueError(f"{context}: event_headers length must match events")
        semantic_value = group.get("semantic_headers", [])
        semantic_headers = require_string_list(
            semantic_value, f"{context}.semantic_headers"
        )
        if semantic_headers and len(semantic_headers) != len(events):
            raise ValueError(f"{context}: semantic_headers length must match events")

        derived_value = group.get("derived")
        if not isinstance(derived_value, list):
            raise ValueError(f"{context}.derived must be a list")
        derived: list[DerivedMetric] = []
        available_tokens = set(events)
        for metric_index, metric_value in enumerate(derived_value):
            metric_context = f"{context}.derived[{metric_index}]"
            metric = require_mapping(metric_value, metric_context)
            metric_name = require_string(metric.get("name"), f"{metric_context}.name")
            number_format = require_string(
                metric.get("number_format"),
                f"{metric_context}.number_format",
            )
            formula = require_string(metric.get("formula"), f"{metric_context}.formula")
            unknown_tokens = set(FORMULA_TOKEN.findall(formula)) - available_tokens
            if unknown_tokens:
                raise ValueError(
                    f"{metric_context}: unknown formula tokens {sorted(unknown_tokens)}"
                )
            derived.append(DerivedMetric(metric_name, number_format, formula))
            available_tokens.add(metric_name)
        groups.append(
            GroupSpec(
                name=name,
                suffix=suffix,
                events=events,
                event_headers=event_headers,
                semantic_headers=semantic_headers,
                derived=tuple(derived),
            )
        )

    names = [group.name for group in groups]
    suffixes = [group.suffix for group in groups]
    if len(names) != len(set(names)) or len(suffixes) != len(set(suffixes)):
        raise ValueError(f"{path}: group names and suffixes must be unique")
    return tuple(groups)


def read_dict_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {key: value or "" for key, value in row.items() if key is not None}
            for row in csv.DictReader(handle)
        ]


def read_matrix(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [list(row) for row in csv.reader(handle)]


def load_wall_times(run_root: Path, stage: str) -> dict[int, float]:
    rows = read_dict_rows(run_root / "time" / "raw" / f"{stage}.csv")
    return {
        int(row["sequence"]): float(row["wall_time_us"])
        for row in rows
        if row["valid"] == "1"
    }


def load_stage_rows(run_root: Path, group: GroupSpec, stage: str) -> list[StageRow]:
    raw_path = run_root / group.name / "raw" / f"{stage}.csv"
    parsed_path = run_root / group.name / "parsed" / f"{stage}.csv"
    raw_rows = read_dict_rows(raw_path)
    parsed_rows = read_dict_rows(parsed_path)
    if not parsed_rows:
        raise ValueError(f"{parsed_path}: no aligned decode rows")
    decode_calls = {int(row["global_call"]) for row in parsed_rows}
    first_decode_call = min(decode_calls)
    wall_times = load_wall_times(run_root, stage)

    rows: list[StageRow] = []
    for raw in raw_rows:
        sequence = int(raw["sequence"])
        global_call = int(raw["global_call"])
        if global_call in decode_calls:
            scope = "Decode（计入汇总）"
        elif global_call < first_decode_call:
            scope = "Prefill（不计入汇总）"
        else:
            scope = "非对齐（不计入汇总）"
        counts = {event: int(raw[event]) for event in group.events}
        rows.append(
            StageRow(
                sequence=sequence,
                global_call=global_call,
                wall_time_us=wall_times.get(sequence),
                time_enabled=int(raw["time_enabled"]),
                time_running=int(raw["time_running"]),
                valid=int(raw["valid"]),
                scope=scope,
                counts=counts,
            )
        )
    if not rows:
        raise ValueError(f"{raw_path}: no raw rows")
    return rows


def style_detail_header(cell: Cell) -> None:
    cell.font = Font(name="微软雅黑", size=11, bold=True, color=WHITE)
    cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
    cell.border = HEADER_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)


def style_detail_body(cell: Cell) -> None:
    cell.font = Font(name="微软雅黑", size=11, color=BLACK)
    cell.fill = PatternFill("solid", fgColor=WHITE)
    cell.border = BODY_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center")


def protect_hex_header(value: str) -> str:
    if re.fullmatch(r"0x[0-9a-f]+", value, flags=re.IGNORECASE):
        return f"\u2060{value}"
    return value


def render_formula(
    template: str,
    row_number: int,
    references: dict[str, str],
) -> str:
    formula = template
    for token in FORMULA_TOKEN.findall(template):
        formula = formula.replace(f"{{{token}}}", f"{references[token]}{row_number}")
    return formula


def write_detail_section(
    worksheet: Worksheet,
    run_root: Path,
    group: GroupSpec,
    stage: str,
    start_row: int,
) -> int:
    profile_path = run_root / "collection_profile"
    if profile_path.is_file() and profile_path.read_text().strip() == "end_to_end":
        worksheet.cell(start_row, 1, stage)
        worksheet.cell(start_row + 1, 1, "未采集（仅端到端模式）")
        return start_row + 3
    rows = load_stage_rows(run_root, group, stage)
    header_rows = 2 if group.semantic_headers else 1
    body_start = start_row + header_rows
    headers = (
        *BASE_HEADERS,
        *(protect_hex_header(value) for value in group.event_headers),
        *(metric.name for metric in group.derived),
    )

    if group.semantic_headers:
        semantic = [None] * len(headers)
        for index, value in enumerate(group.semantic_headers, len(BASE_HEADERS)):
            semantic[index] = value
        for column, value in enumerate(semantic, 1):
            cell = worksheet.cell(start_row, column, value)
            style_detail_header(cell)
        worksheet.row_dimensions[start_row].height = 30
        header_row = start_row + 1
    else:
        header_row = start_row

    for column, value in enumerate(headers, 1):
        cell = worksheet.cell(header_row, column, value)
        style_detail_header(cell)
    worksheet.row_dimensions[header_row].height = 30

    event_start = len(BASE_HEADERS) + 1
    derived_start = event_start + len(group.events)
    event_references = {
        event: get_column_letter(event_start + index)
        for index, event in enumerate(group.events)
    }
    derived_references = {
        metric.name: get_column_letter(derived_start + index)
        for index, metric in enumerate(group.derived)
    }
    references = {**event_references, **derived_references}

    for offset, row in enumerate(rows):
        row_number = body_start + offset
        values: list[object] = [
            stage,
            row.sequence,
            row.global_call,
            row.scope,
            row.wall_time_us,
            row.time_enabled,
            row.time_running,
            row.valid,
            *(row.counts[event] for event in group.events),
        ]
        for column, value in enumerate(values, 1):
            cell = worksheet.cell(row_number, column, value)
            style_detail_body(cell)
        for index, metric in enumerate(group.derived, derived_start):
            cell = worksheet.cell(
                row_number,
                index,
                render_formula(metric.formula, row_number, references),
            )
            style_detail_body(cell)
            cell.number_format = metric.number_format
        worksheet.row_dimensions[row_number].height = 21

        worksheet.cell(row_number, 2).number_format = "0"
        worksheet.cell(row_number, 3).number_format = "0"
        worksheet.cell(row_number, 5).number_format = "0.00"
        for column in range(6, derived_start):
            worksheet.cell(row_number, column).number_format = "0"

    return body_start + len(rows) + 1


def size_detail_sheet(worksheet: Worksheet, last_column: int) -> None:
    widths = {"A": 28, "B": 8, "C": 12, "D": 22, "E": 12, "F": 16, "G": 16, "H": 8}
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width
    for column in range(9, last_column + 1):
        worksheet.column_dimensions[get_column_letter(column)].width = 14
    worksheet.freeze_panes = "B2"
    worksheet.sheet_view.showGridLines = False


def normalize_summary_rows(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        raise ValueError("summary.csv is empty")
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    labels = [
        row[0]
        for row in normalized[1:]
        if row[0] and row[0] not in {"热点函数", "热点函数占比："}
    ]
    if labels != list(SUMMARY_METRICS):
        raise ValueError("summary metrics do not match the 920b template")

    hotspot_index = next(
        (
            index
            for index, row in enumerate(normalized)
            if row and row[0] in {"热点函数", "热点函数占比："}
        ),
        None,
    )
    if hotspot_index is not None and hotspot_index > 0:
        previous = normalized[hotspot_index - 1]
        if any(value for value in previous):
            normalized.insert(hotspot_index, [""] * width)
    return normalized


def summary_value(value: str) -> tuple[object, str]:
    stripped = value.strip()
    if not stripped:
        return None, "General"
    if stripped.endswith("%") and NUMBER.fullmatch(stripped[:-1]):
        return float(stripped[:-1]) / 100, "0.00%"
    if NUMBER.fullmatch(stripped):
        return float(stripped), "0.00"
    return stripped, "General"


def read_frequency_benchmark(run_root: Path) -> list[tuple[str, str]]:
    path = run_root / "frequency" / "benchmark.log"
    if not path.is_file():
        return []

    started = False
    rows: list[tuple[str, str]] = []
    for raw_line in path.read_text(errors="replace").splitlines():
        line = ANSI_ESCAPE.sub("", raw_line)
        if "Serving Benchmark Result" in line:
            started = True
            continue
        if started and line.strip() == "=" * 50:
            break
        if not started:
            continue
        match = BENCHMARK_VALUE.fullmatch(line)
        if match:
            rows.append((match.group("label").strip(), match.group("value")))

    if not started or not rows:
        raise ValueError(f"{path}: benchmark result section was not parsed")
    return rows


def benchmark_value(value: str) -> tuple[int | float, str]:
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value), "0"
    return float(value), "0.00"


def summary_display_label(metric: str) -> str:
    level = SUMMARY_LEVELS.get(metric)
    if metric == "cycle占比" or level is None or level == 1:
        return metric
    return f"{'--' * (level - 1)} {metric}"


def style_summary_row(cell: Cell, metric: str, column_index: int) -> None:
    if not metric:
        cell.font = Font(name="Carlito", size=11)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        return

    level = SUMMARY_LEVELS.get(metric)
    group_start = metric in SUMMARY_GROUP_STARTS
    cell.border = SUMMARY_SECTION_BORDER if group_start else BODY_BORDER
    cell.alignment = Alignment(horizontal="center", vertical="center")

    if level is not None:
        if level <= 2 or column_index == 1:
            cell.fill = PatternFill("solid", fgColor=SUMMARY_LEVEL_FILLS[level])
        if column_index == 1:
            cell.font = Font(
                name="Carlito",
                size=11,
                bold=level <= 2,
                color=SUMMARY_LABEL_FONT,
            )
            cell.alignment = Alignment(
                horizontal="left",
                vertical="center",
                indent=level - 1,
            )
        else:
            cell.font = Font(name="Carlito", size=11, bold=level == 1)
        return

    if column_index == 1:
        cell.font = Font(
            name="Carlito",
            size=11,
            bold=True,
            color=SUMMARY_LABEL_FONT,
        )
        cell.fill = PatternFill("solid", fgColor=SUMMARY_LABEL_FILL)
    else:
        cell.font = Font(name="Carlito", size=11)


def write_summary(worksheet: Worksheet, run_root: Path) -> None:
    rows = normalize_summary_rows(read_matrix(run_root / "summary.csv"))
    benchmark_rows = read_frequency_benchmark(run_root)
    width = len(rows[0])
    for row_index, row in enumerate(rows, 1):
        for column_index, raw_value in enumerate(row, 1):
            value, number_format = summary_value(raw_value)
            if row_index > 1 and column_index == 1:
                value = summary_display_label(row[0]) or None
            cell = worksheet.cell(row_index, column_index, value)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if row_index == 1:
                cell.font = Font(name="Carlito", size=11, bold=True, color=WHITE)
                cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
                cell.border = HEADER_BORDER
            else:
                style_summary_row(cell, row[0], column_index)
                cell.number_format = number_format
        if row_index > 1:
            worksheet.row_dimensions[row_index].height = 21

    benchmark_header_row = len(rows) + 2
    for column_index, value in enumerate(("Benchmark", "数值"), 1):
        cell = worksheet.cell(benchmark_header_row, column_index, value)
        cell.font = Font(name="Carlito", size=11, bold=True, color=WHITE)
        cell.fill = PatternFill("solid", fgColor=HEADER_FILL)
        cell.border = HEADER_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center")
    worksheet.row_dimensions[benchmark_header_row].height = 24

    display_rows = benchmark_rows or [("状态", "未采集")]
    for offset, (label, raw_value) in enumerate(display_rows, 1):
        row_index = benchmark_header_row + offset
        label_cell = worksheet.cell(row_index, 1, label)
        label_cell.font = Font(
            name="Carlito",
            size=11,
            bold=True,
            color=SUMMARY_LABEL_FONT,
        )
        label_cell.fill = PatternFill("solid", fgColor=SUMMARY_LABEL_FILL)
        label_cell.border = HEADER_BORDER
        label_cell.alignment = Alignment(horizontal="center", vertical="center")

        if benchmark_rows:
            value, number_format = benchmark_value(raw_value)
        else:
            value, number_format = raw_value, "General"
        value_cell = worksheet.cell(row_index, 2, value)
        value_cell.font = Font(name="Carlito", size=11)
        value_cell.border = BODY_BORDER
        value_cell.alignment = Alignment(horizontal="center", vertical="center")
        value_cell.number_format = number_format

    frequency_path = run_root / "frequency" / "summary.json"
    if frequency_path.is_file():
        frequency = json.loads(frequency_path.read_text())
        first = benchmark_header_row + len(display_rows) + 2
        frequency_rows = [
            ("独立推理轮频率", "平均值 (MHz)"),
            ("Worker Core", frequency["core_mhz"]),
            (f"NUMA {frequency['numa_node']} Uncore", frequency["uncore_mhz"]),
        ]
        for offset, values in enumerate(frequency_rows):
            for column, value in enumerate(values, 1):
                cell = worksheet.cell(first + offset, column, value)
                reference = worksheet.cell(benchmark_header_row + (offset > 0), column)
                cell._style = copy(reference._style)
                if offset and column == 2:
                    cell.number_format = "0.00"
            worksheet.row_dimensions[first + offset].height = 24 if offset == 0 else 21

    worksheet.row_dimensions[1].height = 24
    benchmark_labels = [label for label, _value in display_rows]
    max_label = max(
        *(len(summary_display_label(str(row[0]))) for row in rows if row),
        *(len(label) for label in benchmark_labels),
        len("Benchmark"),
    )
    worksheet.column_dimensions["A"].width = max(23, min(36, max_label + 2))
    stage_widths = {
        "prepare_attn_model_state": 24,
        "postprocess_sampled": 20,
        END_TO_END_STAGE: 31,
    }
    for column_index in range(2, width + 1):
        header = str(rows[0][column_index - 1])
        column = get_column_letter(column_index)
        worksheet.column_dimensions[column].width = stage_widths.get(header, 17)
    worksheet.freeze_panes = "B2"
    worksheet.sheet_view.showGridLines = False


def hotspot_path(run_root: Path) -> Path:
    symbolized = run_root / "hotspot" / "perf_report_container_symbols.txt"
    if symbolized.is_file():
        return symbolized
    return run_root / "hotspot" / "perf_report.txt"


def write_hotspot(worksheet: Worksheet, run_root: Path) -> None:
    profile_path = run_root / "collection_profile"
    if profile_path.is_file() and profile_path.read_text().strip() == "end_to_end":
        lines = ["未采集（仅端到端模式）"]
    else:
        lines = hotspot_path(run_root).read_text(errors="replace").splitlines()
    header = worksheet.cell(1, 1, "perf report 容器内符号解析输出")
    header.font = Font(name="Carlito", size=11, bold=True, color=WHITE)
    header.fill = PatternFill("solid", fgColor=HEADER_FILL)
    header.border = HEADER_BORDER
    header.alignment = Alignment(horizontal="center", vertical="center")
    worksheet.row_dimensions[1].height = 24
    for row_index, line in enumerate(lines, 2):
        cell = worksheet.cell(row_index, 1, line)
        cell.font = Font(name="Menlo", size=9, color="FF222222")
        cell.alignment = Alignment(
            horizontal="left", vertical="bottom", wrap_text=False
        )
    worksheet.column_dimensions["A"].width = 120
    worksheet.freeze_panes = "B2"
    worksheet.sheet_view.showGridLines = False


def safe_segment(value: str, name: str) -> str:
    if SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsafe filename characters: {value!r}")
    return value


def output_path(args: argparse.Namespace) -> Path:
    if args.input_len <= 0 or args.output_len <= 0:
        raise ValueError("input and output lengths must be positive")
    input_tag = (
        f"{args.input_len // 1000}k"
        if args.input_len % 1000 == 0
        else str(args.input_len)
    )
    chip = safe_segment(args.chip, "chip")
    version = safe_segment(args.version, "version")
    model_short = safe_segment(args.model_short, "model-short")
    return args.run_root / (
        f"{chip}_vllm{version}_{model_short}_{input_tag}{args.output_len}.xlsx"
    )


def expected_sheet_names(
    groups: tuple[GroupSpec, ...],
    include_end_to_end: bool = False,
    include_zen1: bool = False,
) -> list[str]:
    names = ["汇总", "热点函数"]
    for group in groups:
        names.extend(f"{stem} {group.suffix}" for stem, _ in STAGE_SHEETS)
        if include_end_to_end:
            names.append(f"{END_TO_END_SHEET} {group.suffix}")
    if include_zen1:
        names.append("Zen1诊断（待验证）")
    return names


def validate_saved_workbook(
    path: Path,
    groups: tuple[GroupSpec, ...],
    include_end_to_end: bool = False,
    include_zen1: bool = False,
) -> None:
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        expected = expected_sheet_names(groups, include_end_to_end, include_zen1)
        if workbook.sheetnames != expected:
            raise ValueError(f"unexpected worksheet order in {path}")
        for worksheet in workbook.worksheets:
            if worksheet.freeze_panes != "B2":
                raise ValueError(f"{path}: {worksheet.title} freeze pane is not B2")
        if workbook["热点函数"].max_row < 2:
            raise ValueError(f"{path}: hotspot report is empty")
        summary_labels = {
            workbook["汇总"].cell(row, 1).value
            for row in range(1, workbook["汇总"].max_row + 1)
        }
        if "Benchmark" not in summary_labels:
            raise ValueError(f"{path}: benchmark section is missing")
    finally:
        workbook.close()


def write_zen1_diagnostics(worksheet: Worksheet, path: Path) -> None:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.reader(handle))
    for row_index, row in enumerate(rows, 1):
        for column, raw in enumerate(row, 1):
            value, number_format = summary_value(raw)
            cell = worksheet.cell(row_index, column, value)
            if row_index == 1:
                style_detail_header(cell)
            else:
                style_summary_row(cell, row[0], column)
            cell.number_format = number_format
            cell.alignment = Alignment(
                horizontal="left" if column in (1, len(row)) else "center",
                vertical="center",
                wrap_text=True,
            )
        worksheet.row_dimensions[row_index].height = 48 if row_index == 1 else 42
    worksheet.column_dimensions["A"].width = 30
    for column in range(2, len(rows[0])):
        worksheet.column_dimensions[get_column_letter(column)].width = 24
    worksheet.column_dimensions[get_column_letter(len(rows[0]))].width = 60
    worksheet.freeze_panes = "B2"
    worksheet.sheet_view.showGridLines = False


HOTSPOT_ROW = re.compile(r"^\s*([\d.]+)%\s+(\S+)\s+(\S+)\s+(.+?)\s*$")


def export_details(root: Path) -> None:
    target = root / "details"
    target.mkdir(exist_ok=True)
    entries = []
    for source in sorted(root.rglob("*.csv")):
        relative = source.relative_to(root)
        if relative.parts[0] in {"details", "spe", "evidence"} or any(
            part.startswith(".") for part in relative.parts
        ):
            continue
        destination = target / "topdown" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        entries.append(
            {
                "source": str(relative),
                "csv": str(destination.relative_to(root)),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
    (target / "index.json").write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n"
    )


def write_compact_hotspot(worksheet: Worksheet, source: Path, root: Path) -> None:
    rows = []
    for line in source.read_text(errors="replace").splitlines():
        match = HOTSPOT_ROW.match(line)
        if match:
            share, command, library, symbol = match.groups()
            rows.append((float(share) / 100, command, library, symbol))
    if not rows:
        raise ValueError(f"No hotspot records in {source}")
    headers = ("采样占比", "线程", "代码库", "函数")
    destination = root / "details/hotspot.csv"
    destination.parent.mkdir(exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerows(rows)
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    for cell in worksheet["A"][1:]:
        cell.number_format = "0.00%"
    for column, width in zip("ABCD", (16, 24, 40, 90), strict=True):
        worksheet.column_dimensions[column].width = width
    worksheet.auto_filter.ref = worksheet.dimensions


def build_workbook(args: argparse.Namespace, groups: tuple[GroupSpec, ...]) -> Path:
    include_end_to_end = getattr(args, "include_end_to_end", False)
    workbook = Workbook()
    workbook.remove(workbook.active)
    summary = workbook.create_sheet("汇总")
    hotspot = workbook.create_sheet("热点函数")
    write_summary(summary, args.run_root)
    write_hotspot(hotspot, args.run_root)

    compact = getattr(args, "compact_report", False)
    if compact:
        export_details(args.run_root)
        summary.title = "Topdown"
        workbook.remove(hotspot)
        hotspot = workbook.create_sheet("Hotspot")
        if (args.run_root / "collection_profile").read_text().strip() == "full":
            write_compact_hotspot(hotspot, hotspot_path(args.run_root), args.run_root)
        else:
            hotspot.append(["未采集（仅端到端模式）"])
        for row in hotspot:
            for cell in row:
                if cell.row == 1:
                    reference = summary.cell(1, 2)
                else:
                    reference = summary.cell(4, 2)
                    if cell.column == 1:
                        cell.number_format = "0.00%"
                cell.font = copy(reference.font)
                cell.fill = copy(reference.fill)
                cell.border = copy(reference.border)
                cell.alignment = Alignment(
                    horizontal=(
                        "center"
                        if cell.row == 1
                        else "right"
                        if cell.column == 1
                        else "left"
                    ),
                    vertical="center",
                    indent=1,
                )
        hotspot.freeze_panes = "B2"
        hotspot.sheet_view.showGridLines = False
        hotspot.row_dimensions[1].height = 24

    for group in () if compact else groups:
        last_column = len(BASE_HEADERS) + len(group.events) + len(group.derived)
        for stem, stages in STAGE_SHEETS:
            worksheet = workbook.create_sheet(f"{stem} {group.suffix}")
            start_row = 1
            for stage in stages:
                start_row = write_detail_section(
                    worksheet,
                    args.run_root,
                    group,
                    stage,
                    start_row,
                )
            size_detail_sheet(worksheet, last_column)
        if include_end_to_end:
            worksheet = workbook.create_sheet(f"{END_TO_END_SHEET} {group.suffix}")
            write_detail_section(
                worksheet,
                args.run_root / "end_to_end",
                group,
                END_TO_END_STAGE,
                1,
            )
            size_detail_sheet(worksheet, last_column)

    diagnostics = args.run_root / "zen1_diagnostics.csv"
    include_zen1 = diagnostics.is_file()
    if include_zen1 and not compact:
        write_zen1_diagnostics(workbook.create_sheet("Zen1诊断（待验证）"), diagnostics)
    workbook.active = 0
    workbook.calculation.calcMode = "auto"
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    output = output_path(args)
    workbook.save(output)
    workbook.close()
    if not compact:
        validate_saved_workbook(output, groups, include_end_to_end, include_zen1)
    else:
        with ZipFile(output) as archive:
            if archive.testzip() is not None:
                raise ValueError("Invalid compact workbook archive")
    spe_dir = getattr(args, "spe_dir", None) or args.run_root / "spe" / "analysis"
    if not getattr(args, "defer_spe", False) and (
        getattr(args, "spe_dir", None) is not None or spe_dir.exists()
    ):
        report = (
            getattr(args, "spe_report", None)
            or Path(__file__).parent / "spe" / "report.py"
        )
        if not report.is_file():
            raise FileNotFoundError(f"SPE report helper is missing: {report}")
        subprocess.run(
            [
                sys.executable,
                str(report),
                "--workbook",
                str(output),
                "--spe-dir",
                str(spe_dir),
            ],
            check=True,
        )
    return output


def main() -> int:
    args = parse_args()
    groups = load_config(args.config)
    output = build_workbook(args, groups)
    LOGGER.info("wrote %s", output)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
