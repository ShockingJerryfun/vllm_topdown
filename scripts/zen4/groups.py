# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Prepare Zen 4 event groups for the selected Worker CPUs."""

import json
import os
import sys
from pathlib import Path


def cpu_list(value: str) -> list[int]:
    result = set()
    for part in value.split(","):
        ends = [int(item) for item in part.split("-")]
        if len(ends) > 2 or ends[-1] < ends[0]:
            raise ValueError(f"Invalid CPU list: {value}")
        result.update(range(ends[0], ends[-1] + 1))
    return sorted(result)


def l3_group(cpu_root: Path, pmu_root: Path, workers: list[int]) -> tuple[dict, int]:
    if not 1 <= len(workers) <= 2:
        raise ValueError("Filtered Zen 4 L3 collection requires one or two Worker CPUs")
    formats = {
        "event": "config:0-7",
        "umask": "config:8-15",
        "coreid": "config:42-44",
        "enallslices": "config:46",
        "threadmask": "config:56-57",
    }
    for name, expected in formats.items():
        if (pmu_root / "format" / name).read_text().strip() != expected:
            raise ValueError(f"Unexpected L3 PMU format: {name}")
    domains = []
    codes, names, headers = [], [], []
    cores = set()
    for index, cpu in enumerate(workers):
        base = cpu_root / f"cpu{cpu}"
        siblings = cpu_list(
            (base / "topology/thread_siblings_list").read_text().strip()
        )
        core = int((base / "topology/core_id").read_text().strip())
        if (
            not siblings
            or cpu != siblings[0]
            or len(siblings) > 2
            or not 0 <= core <= 7
            or core in cores
        ):
            raise ValueError(f"CPU {cpu}: unsupported or repeated L3 core identifier")
        cores.add(core)
        thread_mask = 1 << siblings.index(cpu)
        caches = [
            item
            for item in (base / "cache").glob("index*")
            if (item / "level").read_text().strip() == "3"
            and (item / "type").read_text().strip() == "Unified"
        ]
        if len(caches) != 1:
            raise ValueError(f"CPU {cpu}: unified L3 topology unavailable")
        domains.append(
            set(cpu_list((caches[0] / "shared_cpu_list").read_text().strip()))
        )
        for label, umask in (("access", 0xFF), ("miss", 0x01)):
            code = (thread_mask << 56) | (1 << 46) | (core << 42) | (umask << 8) | 4
            names.append(f"l3_{label}_{index}")
            codes.append(hex(code))
            headers.append(f"CPU {cpu} L3 {label}")
    if any(domain != domains[0] for domain in domains):
        raise ValueError("Filtered L3 Worker CPUs must share one L3 domain")
    representatives = domains[0] & set(
        cpu_list((pmu_root / "cpumask").read_text().strip())
    )
    if not representatives:
        raise ValueError("No L3 PMU representative in the Worker L3 domain")
    codes.append("0xc0")
    names.append("0xc0")
    headers.append("instructions (Worker thread)")
    return {
        "name": "common_l3",
        "suffix": "L3_SHARED",
        "events": names,
        "raw_events": codes,
        "event_headers": headers,
        "derived": [],
        "scope": "uncore",
    }, min(representatives)


def main() -> None:
    root = Path(sys.argv[1])
    config = json.loads(Path(__file__).with_name("report_config.json").read_text())
    if os.environ.get("ZEN4_L3_ENABLE", "0") == "1":
        first_cpu = Path("/proc/cpuinfo").read_text().split("\n\n", 1)[0]
        fields = dict(
            line.split(":", 1) for line in first_cpu.splitlines() if ":" in line
        )
        model = next(
            (value.strip() for key, value in fields.items() if key.strip() == "model"),
            "",
        )
        if model != "17":
            raise ValueError("Filtered L3 mapping is validated for Zen 4 model 17")
        group, cpu = l3_group(
            Path("/sys/devices/system/cpu"),
            Path("/sys/bus/event_source/devices/amd_l3"),
            cpu_list(os.environ["WORKER_CPUS"]),
        )
        config["groups"].append(group)
        (root / "l3_cpu.txt").write_text(f"{cpu}\n")
    (root / "report_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    )
    for group in config["groups"]:
        print(
            "|".join(
                (
                    group["name"],
                    ",".join(group.get("raw_events", group["events"])),
                    ",".join(group["events"]),
                    group.get("scope", "thread"),
                )
            )
        )


if __name__ == "__main__":
    main()
