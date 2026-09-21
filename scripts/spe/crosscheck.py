# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Independent perf text reconciliation, without borrowing perf's field labels."""

import gzip
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

if __package__:
    from .records import PerfData
else:
    from records import PerfData


PACKET_LINE = re.compile(r"^\.\s+([0-9a-f]+):\s*((?:[0-9a-f]{2}\s+)+)(.*?)\s*$")
MEMORY_LINE = re.compile(
    r"^\s*(\d+)/(\d+)\s+(\d+)\.(\d+):\s+([0-9a-f]+)\s+([0-9a-f]+)"
    r"\s+\|.*\s+(\d+)\s+([0-9a-f]+)\s+([0-9a-f]+)$"
)

MEMORY_CPU_LINE = re.compile(
    MEMORY_LINE.pattern.replace(r"(\d+)/(\d+)\s+", r"(\d+)/(\d+)\s+\[(\d+)\]\s+", 1)
)


@contextmanager
def text_stream(path: Path) -> Iterator[TextIO]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt") as source:
            yield source
    else:
        with path.open() as source:
            yield source


def check_packet_dump(path: Path, perf: PerfData) -> dict[str, Any]:
    chunks = sorted(
        (chunk for stream in perf.streams.values() for chunk in stream),
        key=lambda chunk: chunk.file_offset,
    )
    chunk_number = -1
    position = count = 0
    active = False
    with text_stream(path) as source:
        for line in source:
            if "PERF_RECORD_LOST" in line or "Bad packet!" in line:
                raise ValueError("Independent perf dump reported loss or bad packets")
            if "ARM SPE data: size" in line:
                if active:
                    raise ValueError(
                        "Incomplete AUXTRACE chunk in independent packet dump"
                    )
                chunk_number += 1
                if chunk_number >= len(chunks):
                    raise ValueError("Extra AUXTRACE chunk in independent packet dump")
                position, active = 0, True
                continue
            if not active:
                continue
            match = PACKET_LINE.match(line)
            if match is None:
                if line.strip():
                    raise ValueError("Unparsed line inside independent SPE packet dump")
                continue
            raw = bytes.fromhex(match[2])
            chunk = chunks[chunk_number]
            start = chunk.file_offset + position
            if (
                int(match[1], 16) != position
                or perf.data[start : start + len(raw)] != raw
            ):
                raise ValueError("Independent SPE packet dump bytes/offset disagree")
            position += len(raw)
            count += len(raw)
            if position > chunk.size:
                raise ValueError("Independent SPE dump exceeds AUXTRACE chunk")
            active = position != chunk.size
    if active or chunk_number + 1 != len(chunks):
        raise ValueError("Independent SPE packet dump does not cover all chunks")
    return {
        "status": "pass" if chunks else "not_applicable",
        "chunks": len(chunks),
        "bytes": count,
        "method": "Every perf dump AUX byte matched its original file chunk",
    }


def memory_key(
    row: dict[str, Any], include_cpu: bool = False
) -> tuple[int, ...] | None:
    if any(
        row.get(field) is None for field in ("pc", "va", "pa", "tot", "time_ns", "tid")
    ):
        return None
    key = (
        row["tid"],
        int(row["pc"], 16),
        int(row["va"], 16),
        int(row["pa"], 16),
        row["tot"],
        int(row["time_ns"]) // 1000,
    )
    return (row["cpu"],) + key if include_cpu else key


def check_memory_dump(
    path: Path, expected: Counter[tuple[int, ...]], format_name: str = "perf_memory"
) -> dict[str, Any]:
    actual: Counter[tuple[int, ...]] = Counter()
    rows = 0
    with text_stream(path) as source:
        for line in source:
            if not line.strip() or line.startswith("#"):
                continue
            if format_name == "fields":
                fields = line.split()
                if len(fields) != 8:
                    raise ValueError(
                        "Expected eight fields in independent SPE memory dump"
                    )
                _, tid, cpu, stamp, pc, va, pa, weight = fields
                seconds, fraction = stamp.rstrip(":").split(".")
                key = (
                    int(cpu.strip("[]")),
                    int(tid),
                    int(pc, 16),
                    int(va, 16),
                    int(pa, 16),
                    int(weight),
                    int(seconds) * 1000000 + int((fraction + "000000")[:6]),
                )
            elif format_name in {"perf_memory", "perf_memory_cpu"}:
                pattern = (
                    MEMORY_CPU_LINE if format_name == "perf_memory_cpu" else MEMORY_LINE
                )
                match = pattern.match(line)
                if match is None:
                    raise ValueError("Unparsed independent SPE memory line")
                fields = list(match.groups())
                cpu = int(fields.pop(2)) if format_name == "perf_memory_cpu" else None
                _, tid, seconds, fraction, va, _, weight, pc, pa = fields
                key = (() if cpu is None else (cpu,)) + (
                    int(tid),
                    int(pc, 16),
                    int(va, 16),
                    int(pa, 16),
                    int(weight),
                    int(seconds) * 1000000 + int((fraction + "000000")[:6]),
                )
            else:
                raise ValueError(f"Unsupported memory dump format: {format_name}")
            rows += 1
            if key in expected:
                actual[key] += 1
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise ValueError(
            f"Independent perf memory mismatch: "
            f"missing={sum(missing.values())}, extra={sum(extra.values())}"
        )
    return {
        "status": "pass" if expected else "not_applicable",
        "all_rows": rows,
        "selected_matched": sum(expected.values()),
        "cpu_checked": format_name in {"fields", "perf_memory_cpu"},
        "method": "TID/PC/VA/PA/TOT/converted timestamp_us multiplicities",
    }
