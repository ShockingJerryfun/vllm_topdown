"""Export the vLLM benchmark result sections."""

import csv
import re
from pathlib import Path


VALUE = re.compile(r"^\s*(.+?):\s*([-+]?\d+(?:\.\d+)?)\s*$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def export_benchmark(bench: Path, output: Path) -> None:
    rows = []
    for phase in ("benchmark_before", "benchmark_after"):
        source = bench / phase / "benchmark.log"
        if not source.is_file():
            continue
        started = False
        for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            line = ANSI_ESCAPE.sub("", line)
            if "Serving Benchmark Result" in line:
                started = True
                continue
            if started and line.strip() == "=" * 50:
                break
            match = VALUE.fullmatch(line) if started else None
            if match:
                rows.append((phase, match.group(1).strip(), float(match.group(2))))
    if rows:
        output.mkdir(parents=True, exist_ok=True)
        with (output / "benchmark.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("round", "metric", "value"))
            writer.writerows(rows)
