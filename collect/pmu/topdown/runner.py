"""Run an instrumented command once per PMU group and summarize regions."""

import csv
import json
import os
import platform
import subprocess
import time
from pathlib import Path

from .profiles import zen4
from analyze.pmu.topdown.probe.summary import write_summary
from .records import parse_log, write_stage_rows


def load_groups(profile: str) -> dict[str, list[str]]:
    if profile not in ("920b", "zen4"):
        raise ValueError(f"Unsupported CPU profile: {profile}")
    path = Path(__file__).parent / "profiles" / f"{profile}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_platform(profile: str, system: str, machine: str, cpuinfo: str) -> None:
    """Reject a CPU architecture that cannot use the selected raw events."""
    if profile == "920b":
        if system != "Linux" or machine != "aarch64":
            raise RuntimeError("The 920B PMU profile requires Linux on aarch64")
        return
    if profile != "zen4":
        raise ValueError(f"Unsupported CPU profile: {profile}")
    if system != "Linux" or machine != "x86_64":
        raise RuntimeError("The Zen 4 PMU profile requires Linux on x86_64")
    fields = {}
    for line in cpuinfo.split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    try:
        family = int(fields.get("cpu family", ""))
        model = int(fields.get("model", ""))
    except ValueError as error:
        raise RuntimeError("Cannot identify the AMD CPU family and model") from error
    # Linux perf maps known family 19h Zen 3 models before its Zen 4 fallback.
    if (fields.get("vendor_id") != "AuthenticAMD" or family != 25
            or model < 0x10 or model >> 4 in (0x2, 0x4, 0x5)):
        raise RuntimeError("The Zen 4 profile requires an AMD Zen 4 family 19h CPU")


def collect(
    command: list[str], root: Path, profile: str, discard: int, expected: int | None
) -> dict:
    if not command:
        raise ValueError("An instrumented workload command is required")
    if discard < 0 or (expected is not None and expected <= 0):
        raise ValueError("Discard and expected sample counts must be non-negative")
    groups = load_groups(profile)
    if platform.system() != "Linux":
        validate_platform(profile, platform.system(), platform.machine(), "")
    cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    validate_platform(profile, platform.system(), platform.machine(), cpuinfo)
    root.mkdir(parents=True, exist_ok=False)
    (root / "cpuinfo.txt").write_text(cpuinfo, encoding="utf-8")
    metadata = {
        "profile": profile, "command": command, "platform": platform.platform(),
        "machine": platform.machine(), "scope": "calling thread",
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "frequency_semantics": "Cycles and time are from separate rounds; frequency is an estimate.",
        "discard_per_region": discard, "expected_samples": expected,
        "groups": groups, "started_ns": time.time_ns(), "rounds": [],
    }
    if profile == "zen4":
        metadata["metric_semantics"] = zen4.REPORT_NOTE
        metadata["frequency_semantics"] = zen4.FREQUENCY_NOTE
    stages = None
    sample_counts = {}
    quality = []
    for label, events in [("time", []), *groups.items()]:
        mode = "time" if label == "time" else "pmu"
        folder = root / label
        folder.mkdir()
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (package_root, env.get("PYTHONPATH", "")) if part
        )
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(KPERF_ENABLE="1", KPERF_CONTROL="0", KPERF_MODE=mode,
                   KPERF_SCOPE="thread", KPERF_RAW_EVENTS=",".join(events),
                   KPERF_EVENT_NAMES=",".join(events), KPERF_TARGET="",
                   KPERF_QUALIFIER="", KPERF_STRICT_NUMA="1",
                   KPERF_RUNTIME_IDENTITY_DIR=str(folder / "runtime_identity"))
        log = folder / "measurement.log"
        with log.open("w", encoding="utf-8") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, env=env)
            returncode = process.wait()
        metadata["rounds"].append({"group": label, "pid": process.pid, "returncode": returncode})
        (root / "run.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if returncode:
            raise RuntimeError(f"Workload failed in {label}; see {log}")
        identity_files = list((folder / "runtime_identity").glob("*.json"))
        if len(identity_files) != 1:
            raise RuntimeError(f"Expected one instrumented process in {label}; see {log}")
        identity = json.loads(identity_files[0].read_text(encoding="utf-8"))
        if identity["pid"] != process.pid:
            raise RuntimeError(f"Unexpected instrumented process in {label}")
        rows = parse_log(log, events, mode)
        if not rows:
            raise RuntimeError(f"No measured regions in {label}; see {log}")
        if stages is None:
            stages = list(rows)
        elif set(rows) != set(stages):
            raise RuntimeError(f"Measured regions differ in {label}")
        for stage in stages:
            all_rows = rows[stage]
            selected = [dict(row, sequence=index) for index, row in
                        enumerate(all_rows[discard:], 1)]
            if not selected or (expected is not None and len(selected) != expected):
                raise RuntimeError(f"Unexpected sample count for {label}/{stage}")
            if label == "time":
                sample_counts[stage] = len(selected)
            elif len(selected) != sample_counts[stage]:
                raise RuntimeError(f"Sample count differs across rounds for {label}/{stage}")
            if any(row["valid"] != 1 for row in selected):
                raise RuntimeError(f"Invalid or multiplexed sample for {label}/{stage}")
            if mode == "pmu" and any(row["time_running"] <= 0 or
                                     row["time_running"] != row["time_enabled"]
                                     for row in selected):
                raise RuntimeError(f"PMU multiplexing in {label}/{stage}")
            write_stage_rows(folder / "raw" / f"{stage}.csv", all_rows, mode, events)
            write_stage_rows(folder / "parsed" / f"{stage}.csv", selected, mode, events)
            quality.append([label, stage, len(all_rows), len(selected), "ok"])
    if stages is None:
        raise RuntimeError("No measured regions")
    with (root / "collection_quality.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["group", "stage", "raw", "selected", "status"])
        writer.writerows(quality)
    write_summary(root, stages, profile)
    metadata["finished_ns"] = time.time_ns()
    metadata["status"] = "complete"
    (root / "run.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return metadata
