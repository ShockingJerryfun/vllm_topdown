#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Validate explicit Linux placement and audit only this service's process tree."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import regex as re


def cpu_list(value: str) -> set[int]:
    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
        raise ValueError(f"Invalid CPU list: {value!r}")
    result = set()
    for part in value.split(","):
        ends = [int(item) for item in part.split("-")]
        low, high = ends[0], ends[-1]
        if high < low:
            raise ValueError("Reversed CPU range")
        new = set(range(low, high + 1))
        if result & new:
            raise ValueError("Duplicate CPU in list")
        result.update(new)
    return result


def topology(cpu: int) -> tuple[int, int, int, set[int]]:
    base = Path(f"/sys/devices/system/cpu/cpu{cpu}")
    package = int((base / "topology/physical_package_id").read_text())
    cluster = int((base / "topology/cluster_id").read_text())
    if cluster < 0:
        raise ValueError("Unknown cluster topology")
    nodes = list(base.glob("node[0-9]*"))
    if len(nodes) != 1:
        raise ValueError(f"CPU {cpu}: NUMA topology unavailable")
    siblings = cpu_list((base / "topology/thread_siblings_list").read_text().strip())
    return package, cluster, int(nodes[0].name[4:]), siblings


def validate_config() -> dict[str, object]:
    sets = {
        name: cpu_list(os.environ[name])
        for name in ("WORKER_CPUS", "WORKER_POOL_CPUS", "SERVICE_CPUS", "CLIENT_CPUS")
    }
    worker, pool, service, client = (sets[name] for name in sets)
    node = int(os.environ["WORKER_NUMA_NODE"])
    if len(pool) != 4 or len(worker) not in (1, 2, 4) or not worker <= pool:
        raise ValueError("Select 1/2/4 CPUs from a fixed four-physical-core pool")
    union = pool | service | client
    allowed = os.sched_getaffinity(0)
    if not union <= allowed:
        raise ValueError(f"Container/launcher CPU allowance missing {union - allowed}")
    info = {cpu: topology(cpu) for cpu in union}
    if len({info[cpu][:3] for cpu in pool}) != 1:
        raise ValueError("Worker pool must share socket, cluster and NUMA node")
    if any(info[cpu][2] != node for cpu in pool):
        raise ValueError("Worker CPUs must use the configured NUMA node")
    if len({frozenset(info[cpu][3]) for cpu in pool}) != 4:
        raise ValueError("Worker pool contains SMT siblings of the same physical core")
    reserved = set().union(*(info[cpu][3] for cpu in pool))
    service_cores = set().union(*(info[cpu][3] for cpu in service))
    if reserved & (service | client) or service_cores & client:
        raise ValueError("Worker pool, service and client physical cores overlap")
    status = Path("/proc/self/status").read_text()
    mems = re.search(r"^Mems_allowed_list:\s*(.+)$", status, re.MULTILINE)
    if mems is None or node not in cpu_list(mems[1].strip()):
        raise ValueError("NUMA node is outside the container memory allowance")
    return {**{key: sorted(value) for key, value in sets.items()}, "node": node}


def process_table() -> dict[int, dict[str, object]]:
    table = {}
    for path in Path("/proc").glob("[0-9]*"):
        try:
            stat = (path / "stat").read_text().rsplit(")", 1)[1].split()
            command = (path / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (FileNotFoundError, ProcessLookupError):
            continue  # Exited during enumeration; selected PIDs are rechecked.
        table[int(path.name)] = {
            "ppid": int(stat[1]),
            "session": int(stat[3]),
            "start": int(stat[19]),
            "command": command,
        }
    return table


def identify(
    api: int, table: dict[int, dict[str, object]], allow_uni: bool = False
) -> dict[str, object]:
    if api not in table or table[api]["session"] != api:
        raise RuntimeError("API session leader disappeared or changed")
    descendants = {api}
    while True:
        children = {pid for pid, item in table.items() if item["ppid"] in descendants}
        if children <= descendants:
            break
        descendants |= children
    members = {pid: table[pid] for pid in descendants}
    if any(item["session"] != api for item in members.values()):
        raise RuntimeError("Service descendant escaped its session")
    workers = [
        pid
        for pid, item in members.items()
        if re.fullmatch(r"VLLM::Worker(?:_[A-Z]+[0-9]+)*", str(item["command"]).strip())
    ]
    engines = [
        pid
        for pid, item in members.items()
        if re.fullmatch(r"VLLM::EngineCore(?:_DP[0-9]+)?", str(item["command"]).strip())
    ]
    if allow_uni and not workers and len(engines) == 1:
        workers = engines.copy()
    if len(workers) != 1 or len(engines) != 1:
        raise RuntimeError(
            f"Expected one mp Worker and EngineCore: {workers}, {engines}"
        )
    return {
        "api": api,
        "worker": workers[0],
        "engine": engines[0],
        "starts": {str(pid): item["start"] for pid, item in members.items()},
    }


def page_distribution(text: str, node: int) -> dict[str, object]:
    totals: dict[str, int] = {}
    anonymous: dict[str, int] = {}
    mixed: dict[str, int] = {}
    policies: set[str] = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        policies.add(fields[1])
        pages = {
            key: int(value) for key, value in re.findall(r"\b(N\d+)=(\d+)\b", line)
        }
        anon_match = re.search(r"\banon=(\d+)\b", line)
        anon_pages = int(anon_match[1]) if anon_match else 0
        all_anon = anon_pages > 0 and anon_pages == sum(pages.values())
        for key, count in pages.items():
            totals[key] = totals.get(key, 0) + count
            if all_anon:
                anonymous[key] = anonymous.get(key, 0) + count
            elif anon_pages:
                mixed[key] = mixed.get(key, 0) + count
        if anon_pages and fields[1] != f"bind:{node}":
            raise RuntimeError("Worker anonymous mapping lacks required membind policy")
    if not anonymous or any(
        count for key, count in anonymous.items() if key != f"N{node}"
    ):
        raise RuntimeError(
            f"Worker unambiguously anonymous pages absent or "
            f"outside node {node}: {anonymous}"
        )
    return {
        "all_resident_pages": totals,
        "anonymous_resident_pages": anonymous,
        "mixed_mapping_resident_pages": mixed,
        "mapping_policies": sorted(policies),
        "scope": (
            "N counts cover entire VMAs; mixed file/COW pages require "
            "page-level verification"
        ),
    }


def audit(api: int, identity_path: Path, bind: bool) -> dict[str, object]:
    identity = identify(api, process_table())
    if identity_path.exists():
        previous = json.loads(identity_path.read_text())
        for key in ("api", "worker", "engine"):
            pid = str(identity[key])
            if (
                previous[key] != identity[key]
                or previous["starts"][pid] != identity["starts"][pid]
            ):
                raise RuntimeError(
                    "Service role PID/start-time changed; restart the run"
                )
    elif not bind:
        raise RuntimeError("Missing initial placement identity")
    config = {
        name: os.environ[name]
        for name in (
            "WORKER_CPUS",
            "WORKER_POOL_CPUS",
            "WORKER_NUMA_NODE",
            "SERVICE_CPUS",
            "CLIENT_CPUS",
        )
    }
    if identity_path.exists() and previous.get("config") != config:
        raise RuntimeError(
            "Placement changed within a service session; restart required"
        )
    identity["config"] = config
    worker = int(identity["worker"])
    masks = {}
    for value in identity["starts"]:
        pid = int(value)
        proc = Path(f"/proc/{pid}")
        if proc.stat().st_uid != os.getuid():
            raise RuntimeError("Service descendant has a different owner")
        actual_start = int((proc / "stat").read_text().rsplit(")", 1)[1].split()[19])
        if actual_start != identity["starts"][value]:
            raise RuntimeError("PID reused before affinity operation")
        expected = cpu_list(
            os.environ["WORKER_CPUS" if pid == worker else "SERVICE_CPUS"]
        )
        tids = list(Path(f"/proc/{pid}/task").glob("[0-9]*"))
        if not tids:
            raise RuntimeError(f"No threads for service PID {pid}")
        for task in tids:
            tid = int(task.name)
            if bind and pid != worker:
                os.sched_setaffinity(tid, expected)
            actual = os.sched_getaffinity(tid)
            if actual != expected:
                raise RuntimeError(
                    f"Thread {pid}/{tid} affinity {actual} != {expected}"
                )
            masks[str(tid)] = sorted(actual)
    numa_text = Path(f"/proc/{worker}/numa_maps").read_text()
    if bind:
        identity_path.with_suffix(".numa_maps").write_text(numa_text)
    try:
        pages = page_distribution(numa_text, int(os.environ["WORKER_NUMA_NODE"]))
    except RuntimeError:
        identity_path.with_suffix(".failed_numa_maps").write_text(numa_text)
        raise
    # Detect exit/reuse while reading threads/pages, before persisting acceptance.
    current = identify(api, process_table())
    if any(current[key] != identity[key] for key in current):
        raise RuntimeError("Service process tree changed during audit")
    if not identity_path.exists():
        identity_path.write_text(json.dumps(identity) + "\n")
    return {
        "time_ns": time.time_ns(),
        "identity": identity,
        "threads": masks,
        "pages": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "bind", "check", "worker"))
    parser.add_argument("--api", type=int)
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    if args.action == "preflight":
        result = validate_config()
    elif args.action == "worker":
        result = identify(args.api, process_table(), allow_uni=True)["worker"]
    else:
        result = audit(args.api, args.identity, args.action == "bind")
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
