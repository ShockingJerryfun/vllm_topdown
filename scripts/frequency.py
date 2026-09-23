# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Collect a DevKit aggregate over one independently requested inference."""

import argparse
import csv
import json
import os
import signal
import subprocess
import time
from pathlib import Path


def parse_report(text, cpus, node):
    cores, numa = {}, {}
    section = None
    for line in text.splitlines():
        if "Per NUMA Frequency Table" in line:
            section = "numa"
            numa = {}
        elif "CPU Core Frequency Table" in line:
            section = "cores"
            cores = {}
        elif "Table" in line:
            section = None
        fields = [v.strip() for v in line.split("|")[1:-1]]
        if len(fields) < 3 or not fields[0].isdigit():
            continue
        try:
            if section == "numa":
                numa[int(fields[0])] = float(fields[2])
            elif section == "cores" and len(fields) == 4:
                cores[int(fields[0])] = float(fields[3])
        except ValueError:
            continue
    selected = {cpu: cores[cpu] for cpu in cpus if cpu in cores}
    if not selected:
        raise ValueError("DevKit did not report any selected Worker core frequency")
    return {
        "worker_cpus": cpus,
        "numa_node": node,
        "core_mhz": sum(selected.values()) / len(selected),
        "uncore_mhz": numa.get(node),
        "per_core_mhz": selected,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--devkit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpus", required=True)
    parser.add_argument("--node", type=int, required=True)
    args = parser.parse_args()
    root = args.output
    command = [args.devkit, "tuner", "turbostat", "-d", "300"]
    start = time.time()
    proc = None

    def stop(_signum, _frame):
        raise InterruptedError("Frequency collection cancelled")

    signal.signal(signal.SIGTERM, stop)
    try:
        with (root / "devkit.log").open("w") as stream:
            proc = subprocess.Popen(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                cwd=str(Path(args.devkit).parent)
                if Path(args.devkit).is_absolute()
                else None,
            )
            deadline = time.monotonic() + 30
            while "Starting to collect data" not in (root / "devkit.log").read_text():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("DevKit did not become ready")
                time.sleep(0.1)
            ready = time.time()
            (root / "frequency.ready").touch()
            while not (root / "frequency.stop").exists():
                if proc.poll() is not None or time.time() - start > 240:
                    raise RuntimeError("Frequency request failed to finish")
                time.sleep(0.05)
            end = time.time()
            os.killpg(proc.pid, signal.SIGINT)
            proc.wait(timeout=30)
        result = parse_report(
            (root / "devkit.log").read_text(),
            [int(c) for c in args.cpus.split(",")],
            args.node,
        )
        result.update(
            command=command,
            collector_launched=start,
            collection_started=ready,
            collection_stopped=end,
            scope="independent inference round, warmup excluded",
        )
        (root / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
        with (root / "frequency.csv").open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["scope", "id", "mean_mhz"])
            for cpu, mhz in result["per_core_mhz"].items():
                writer.writerow(["core", cpu, mhz])
            writer.writerow(["uncore_numa", args.node, result["uncore_mhz"]])
    finally:
        if proc is not None and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()


if __name__ == "__main__":
    main()
