"""Parse the stable KPERF record format for arbitrary region names."""

import csv
import re
from pathlib import Path

PMU = re.compile(
    r"KPERF,([^,\r\n]+),(\d+),(\d+),(\d+),([01]),(\d+(?:,\d+)*)\s*$"
)
TIME = re.compile(r"KPERF_TIME,([^,\r\n]+),(\d+),(\d+),(\d+),([01])\s*$")


def parse_log(path: Path, events: list[str], mode: str) -> dict[str, list[dict]]:
    rows: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            match = (TIME if mode == "time" else PMU).search(line)
            if not match:
                continue
            name = match.group(1)
            if name in ("", ".", "..") or any(c in name for c in "/\\\x00"):
                raise ValueError(f"Unsafe region name in {path}:{line_number}")
            stage_rows = rows.setdefault(name, [])
            row = {"sequence": len(stage_rows) + 1, "global_call": int(match.group(2))}
            if mode == "time":
                wall_us = int(match.group(3)) / 1000
                cpu_us = int(match.group(4)) / 1000
                row.update(wall_time_us=wall_us, thread_cpu_time_us=cpu_us,
                           cpu_utilization=cpu_us / wall_us if wall_us > 0 else "",
                           valid=int(match.group(5)))
            else:
                values = [int(value) for value in match.group(6).split(",")]
                if len(values) != len(events):
                    raise ValueError(f"Counter count mismatch in {path}:{line_number}")
                row.update(time_enabled=int(match.group(3)), time_running=int(match.group(4)),
                           valid=int(match.group(5)))
                row.update(zip(events, values, strict=True))
            stage_rows.append(row)
    return rows


def write_stage_rows(path: Path, rows: list[dict], mode: str, events: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["sequence", "global_call"]
    if mode == "time":
        fields += ["wall_time_us", "thread_cpu_time_us", "cpu_utilization", "valid"]
    else:
        fields += ["time_enabled", "time_running", "valid", *events]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
