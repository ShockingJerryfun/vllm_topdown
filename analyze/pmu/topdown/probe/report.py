"""A compact generic workbook for independently named regions."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .layout import SUMMARY_DISPLAY_LABELS, SUMMARY_LEVELS, summary_display_label
from .profiles.kunpeng920b import PERCENT_METRICS
from .profiles import zen4


def write_workbook(
    root: Path, values: dict[str, dict[str, float | None]], profile: str = "920b"
) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Topdown"
    stages = list(values)
    sheet.append(["指标", *stages])
    if profile == "zen4":
        sheet.cell(1, 1).comment = Comment(zen4.REPORT_NOTE, "Topdown")
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.fitToWidth = 0
        sheet.page_setup.fitToHeight = 1
    border = Border(bottom=Side(style="thin", color="FFD9E2F3"))
    level_fills = {1: "FFD9EAF7", 2: "FFEDF4FA", 3: "FFF5F9FC", 4: "FFFAFCFE"}
    for cell in sheet[1]:
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.font = Font(name="Carlito", bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for metric in values[stages[0]]:
        level = zen4.SUMMARY_LEVELS.get(metric) if profile == "zen4" else SUMMARY_LEVELS.get(metric)
        if profile == "zen4":
            label = f"{'--' * (level - 1)} {metric}" if level and level > 1 else metric
            if metric == "频率(MHz)":
                label = zen4.FREQUENCY_LABEL
        else:
            label = summary_display_label(metric)
        row = [label, *(values[stage][metric] for stage in stages)]
        missing = "N/A" if profile == "zen4" else "-"
        sheet.append([missing if value is None else value for value in row])
        if metric == "频率(MHz)":
            sheet.cell(sheet.max_row, 1).comment = Comment(
                zen4.FREQUENCY_NOTE if profile == "zen4"
                else "cycles 与墙钟时间来自不同请求轮次，此值为跨轮近似。",
                "Topdown",
            )
        elif profile == "zen4" and metric in zen4.UNAVAILABLE:
            sheet.cell(sheet.max_row, 1).comment = Comment(
                "Zen 4 未定义或未验证该项；共享域不能归属于单线程。", "Topdown"
            )
        elif profile == "zen4" and metric in {"Fetch Latency Bound", "Fetch Bandwidth Bound"}:
            sheet.cell(sheet.max_row, 1).comment = Comment(
                "此项由同组计数计算，与所列父项来自不同轮次，可能不闭合。", "Topdown"
            )
        elif profile == "zen4" and metric in {"Backend CPU (estimate)", "Backend Memory (estimate)"}:
            sheet.cell(sheet.max_row, 1).comment = Comment(
                "此项与所列父项来自不同轮次，同组计数计算，比例为估算且可能不闭合。", "Topdown"
            )
        elif profile == "zen4" and metric.startswith("L2 I+D+prefetch"):
            sheet.cell(sheet.max_row, 1).comment = Comment(
                "包括 I/D 请求与指定硬件预取；不是纯 L2D 指标。", "Topdown"
            )
        elif "推算未命中率" in metric:
            sheet.cell(sheet.max_row, 1).comment = Comment(
                "由同组 1 - hit/access 推算，不表示 DRAM 访问比例。", "Topdown"
            )
        elif metric in SUMMARY_DISPLAY_LABELS:
            sheet.cell(sheet.max_row, 1).comment = Comment(metric, "Topdown")
        for cell in sheet[sheet.max_row]:
            cell.border = border
            cell.font = Font(
                name="Carlito", size=11,
                bold=(level == 1 or (cell.column == 1 and (level is None or level <= 2))),
                color="FF17365D" if cell.column == 1 else "FF000000",
            )
            cell.alignment = Alignment(
                horizontal="left" if cell.column == 1 else "center", vertical="center",
                indent=(level - 1) if cell.column == 1 and level else 0,
            )
            if level is not None and (cell.column == 1 or level <= 2):
                cell.fill = PatternFill("solid", fgColor=level_fills[level])
            elif level is None and cell.column == 1:
                cell.fill = PatternFill("solid", fgColor="FFD9EAF7")
            if cell.column > 1 and isinstance(cell.value, (float, int)):
                percentages = zen4.PERCENT_METRICS if profile == "zen4" else PERCENT_METRICS
                cell.number_format = "0.00%" if metric in percentages else "0.00"
        sheet.row_dimensions[sheet.max_row].height = 21
    sheet.column_dimensions["A"].width = 35
    for index in range(2, len(stages) + 2):
        sheet.column_dimensions[get_column_letter(index)].width = 20
    sheet.freeze_panes = "B2"
    sheet.sheet_view.showGridLines = False
    path = root / "Topdown.xlsx"
    workbook.save(path)
    workbook.close()
    return path
