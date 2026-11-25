# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Normalize one SPE capture; runnable as a package or copied sibling script."""

import argparse
import gzip
import json
import logging
import re
import struct
import tempfile
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from itertools import pairwise
from pathlib import Path
from statistics import mean, median
from typing import Any

if __package__:
    from .crosscheck import check_memory_dump, check_packet_dump, memory_key
    from .native import Scanner
    from .records import EVENT_NAMES, decode_stream, read_perf
    from .resolve import IDENTITY_FIELDS, file_identity, resolve_pcs
else:
    from crosscheck import check_memory_dump, check_packet_dump, memory_key
    from native import Scanner
    from records import EVENT_NAMES, decode_stream, read_perf
    from resolve import IDENTITY_FIELDS, file_identity, resolve_pcs


LOGGER = logging.getLogger(__name__)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def load_address_evidence(
    root: Path, metadata: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Load optional observed address bounds, without deriving service location."""
    evidence: dict[str, Any] = {"bars": [], "system_ram": [], "numa": [], "maps": []}
    inputs = []
    audit: dict[str, Any] = {}
    for line in (root / metadata["maps_file"]).read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) >= 5:
            start, end = (int(part, 16) for part in fields[0].split("-"))
            evidence["maps"].append((start, end, line))
    for field in ("physical_numa", "pci_resources", "iomem"):
        if not metadata.get(field):
            audit[field] = {"status": "not_provided"}
            continue
        path = root / metadata[field]
        try:
            text = path.read_text()
        except OSError as exc:
            audit[field] = {
                "status": "unavailable",
                "path": str(path),
                "reason": str(exc),
            }
            continue
        inputs.append(file_identity(path))
        if field == "physical_numa":
            value = json.loads(text)
            for interval in value["ranges"]:
                start, end, node = (
                    int(interval[key]) for key in ("start", "end", "node")
                )
                if 0 <= start < end and node >= 0:
                    evidence["numa"].append((start, end, node))
            audit[field] = {
                "status": "read",
                "usable_ranges": len(evidence["numa"]),
                "block_size": str(value["block_size"]),
            }
        elif field == "iomem":
            for line in text.splitlines():
                match = re.fullmatch(
                    r"\s*([0-9a-fA-F]+)-([0-9a-fA-F]+)\s*:\s*System RAM\s*", line
                )
                if match:
                    start, end = int(match[1], 16), int(match[2], 16)
                    # All-zero bounds are the kernel's common redacted view.
                    if end >= start and end > 0:
                        evidence["system_ram"].append((start, end + 1))
            audit[field] = {
                "status": "read",
                "usable_ranges": len(evidence["system_ram"]),
            }
        else:
            gpu_bdf = metadata.get("gpu_bdf", "").lower()
            for device in json.loads(text):
                if not gpu_bdf or device["bdf"].lower() != gpu_bdf:
                    continue
                for number, line in enumerate(device["resource"].splitlines()[:6]):
                    fields = line.split()
                    if len(fields) != 3:
                        continue
                    start, end, flags = (int(part, 16) for part in fields)
                    # Only an assigned memory resource can contain an SPE PA.
                    if end >= start and end > 0 and flags & 0x200:
                        evidence["bars"].append((start, end + 1, gpu_bdf, number))
            audit[field] = {
                "status": "read",
                "gpu_bdf": gpu_bdf or None,
                "usable_ranges": len(evidence["bars"]),
            }
    evidence["indexes"] = {
        key: interval_index(
            [(start, end, start, *rest) for start, end, *rest in values]
            if key == "bars"
            else values
        )
        for key, values in evidence.items()
    }
    return evidence, inputs, audit


def interval_index(ranges: list[tuple]) -> tuple[list[int], list[frozenset]]:
    """Index half-open ranges while preserving duplicate/overlapping evidence."""
    events: dict[int, list[tuple[tuple, int]]] = defaultdict(list)
    for start, end, *value in ranges:
        if start >= end:
            continue
        label = tuple(value)
        events[start].append((label, 1))
        events[end].append((label, -1))
    active: Counter = Counter()
    starts = sorted(events)
    values = []
    for start in starts:
        for label, delta in events[start]:
            active[label] += delta
            if not active[label]:
                del active[label]
        values.append(frozenset(active))
    return starts, values


def interval_values(
    index: tuple[list[int], list[frozenset]], address: int
) -> frozenset:
    starts, values = index
    position = bisect_right(starts, address) - 1
    return values[position] if position >= 0 else frozenset()


def address_context(row: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "physical_resource": "unknown",
        "ram_numa": None,
        "pci_bdf": None,
        "bar_offset": None,
        "data_mapping": None,
    }
    if row["va"] is not None:
        va = int(row["va"], 16)
        mappings = interval_values(evidence["indexes"]["maps"], va)
        if len(mappings) == 1:
            result["data_mapping"] = next(iter(mappings))[0]
    if row["pa"] is None:
        return result
    pa = int(row["pa"], 16)
    bars = interval_values(evidence["indexes"]["bars"], pa)
    if len(bars) == 1:
        start, bdf, number = next(iter(bars))
        result.update(
            physical_resource=f"GPU BAR{number}",
            pci_bdf=bdf,
            bar_offset=hex(pa - start),
        )
    elif bars:
        # Conflicting BAR observations cannot identify one resource.
        return result
    elif interval_values(evidence["indexes"]["system_ram"], pa):
        result["physical_resource"] = "System RAM"
        nodes = interval_values(evidence["indexes"]["numa"], pa)
        if len(nodes) == 1:
            result["ram_numa"] = next(iter(nodes))[0]
    return result


def load_windows(root: Path, metadata: dict[str, Any]) -> list[dict[str, Any]]:
    data = read_json(root / metadata["windows"])
    values = data["windows"] if isinstance(data, dict) else data
    host_tid = int(metadata["host_tid"])
    container_tid = int(metadata.get("container_tid", host_tid))
    windows = []
    identifiers = set()
    for item in values:
        row = {
            field: int(item[field])
            for field in (
                "request",
                "step",
                "tid",
                "start",
                "end",
                "replay_start",
                "replay_end",
            )
        }
        if row["tid"] not in (host_tid, container_tid):
            raise ValueError("Marker TID is outside explicit host/container mapping")
        if not row["start"] <= row["replay_start"] < row["replay_end"] <= row["end"]:
            raise ValueError("Invalid fullgraph/replay marker nesting")
        if (row["request"], row["step"]) in identifiers:
            raise ValueError("Duplicate request/step marker")
        identifiers.add((row["request"], row["step"]))
        for field in ("cpu_start", "cpu_end"):
            if field in item:
                row[field] = int(item[field])
                if row[field] not in metadata["cpus"]:
                    raise ValueError("Marker CPU outside capture CPU set")
        row.update(host_tid=host_tid, container_tid=container_tid)
        windows.append(row)
    windows.sort(key=lambda row: row["start"])
    if any(left["end"] > right["start"] for left, right in pairwise(windows)):
        raise ValueError("Overlapping main-thread fullgraph windows")
    if not any(row["request"] > 0 for row in windows):
        raise ValueError("No formal request windows")
    return windows


def select_window(
    row: dict[str, Any], windows: list[dict[str, Any]], starts: list[int], host_tid: int
) -> tuple[dict[str, Any] | None, str]:
    if row["ticks"] is None:
        return None, "missing_timestamp"
    ticks = int(row["ticks"])
    index = bisect_right(starts, ticks) - 1
    if index < 0 or ticks >= windows[index]["end"]:
        return None, "outside_window"
    if row["tid"] is None:
        return None, "missing_context"
    if row["tid"] != host_tid:
        return None, "other_thread"
    window = windows[index]
    if window["request"] <= 0:
        return None, "warmup"
    return window, "selected"


def summarize(groups: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result = []
    for key, rows in groups.items():
        summary = {field: rows[0][field] for field in IDENTITY_FIELDS}
        summary.update(
            pc_key=key,
            pc=rows[0]["pc"],
            pcs=sorted({r["pc"] for r in rows}),
            samples=len(rows),
            replay_samples=sum(row["replay"] for row in rows),
            cpus=dict(Counter(str(row["cpu"]) for row in rows)),
            tids=dict(Counter(str(row["tid"]) for row in rows)),
            requests=dict(Counter(str(row["request"]) for row in rows)),
            event_present=sum(row["event_bits"] is not None for row in rows),
            event_bits={
                str(bit): sum(bit in row["event_set_bits"] for row in rows)
                for bit in range(32)
            },
            unknown_event_values=dict(
                Counter(
                    row["unknown_event_bits"]
                    for row in rows
                    if row["unknown_event_bits"] is not None
                )
            ),
            operation_counts=dict(
                Counter(row["operation"] or "missing" for row in rows)
            ),
            cache_location="unknown",
            memory_policy="unknown",
            counters={},
        )
        for index in map(str, range(32)):
            values = sorted(
                row["counters"][index] for row in rows if index in row["counters"]
            )
            summary["counters"][index] = {
                "present": len(values),
                "missing": len(rows) - len(values),
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "mean": mean(values) if values else None,
                "median": median(values) if values else None,
                "p95": values[(len(values) * 95 + 99) // 100 - 1] if values else None,
            }
        for field in ("physical_resource", "ram_numa", "pci_bdf", "data_mapping"):
            summary[field + "_counts"] = dict(
                Counter(
                    str(row[field]) if row[field] is not None else "unknown"
                    for row in rows
                )
            )
        result.append(summary)
    return sorted(result, key=lambda row: (-row["samples"], row["pc_key"]))


def normalize_run(
    root: Path,
    metadata_path: Path | None = None,
    output: Path | None = None,
    native_library: Path | None = None,
) -> dict[str, Any]:
    """Write a complete normalized run or a fail audit; never mutate raw inputs."""
    root = root.resolve()
    metadata_path = metadata_path or root / "spe_capture.json"
    output = output or root / "analysis"
    output.mkdir(parents=True, exist_ok=True)
    # Invalidate any prior success before processing. Failed retries cannot leave
    # a stale manifest that a report builder could accept as current success.
    write_json(output / "manifest.json", {"schema_version": 1, "status": "processing"})
    try:
        result = process_run(root, metadata_path, output, native_library)
    except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
        write_json(
            output / "audit.json",
            {"schema_version": 1, "status": "fail", "error": str(exc)},
        )
        write_json(output / "manifest.json", {"schema_version": 1, "status": "fail"})
        raise
    return result


def process_run(
    root: Path,
    metadata_path: Path,
    output: Path,
    native_library: Path | None = None,
) -> dict[str, Any]:
    scanner = Scanner(native_library) if native_library else None
    metadata = read_json(metadata_path)
    if metadata.get("schema_version") != 1 or not metadata.get("perf_files"):
        raise ValueError("Expected nonempty capture metadata schema_version 1")
    metadata["cpus"] = [int(cpu) for cpu in metadata["cpus"]]
    if not metadata["cpus"] or len(metadata["cpus"]) != len(set(metadata["cpus"])):
        raise ValueError("Capture CPU list must be nonempty and unique")
    host_tid = int(metadata["host_tid"])
    if host_tid <= 0:
        raise ValueError("Invalid target host TID")
    thread_capture = any(
        "cpu" in item and item["cpu"] is None for item in metadata["perf_files"]
    )
    if thread_capture and (
        len(metadata["perf_files"]) != 1
        or metadata["perf_files"][0].get("target_tid") != host_tid
    ):
        raise ValueError(
            "A thread-scoped capture requires one perf file targeting the host TID"
        )
    windows = load_windows(root, metadata)
    starts = [window["start"] for window in windows]

    def materialize_record(context: dict[str, Any]) -> bool:
        return select_window(context, windows, starts, host_tid)[0] is not None

    inputs = [
        file_identity(path)
        for path in (
            metadata_path,
            root / metadata["windows"],
            root / metadata["maps_file"],
        )
    ]
    if native_library:
        inputs.append(file_identity(native_library.resolve()))
    audit: dict[str, Any] = {
        "schema_version": 1,
        "status": "processing",
        "files": [],
        "warnings": [],
        "window_count": sum(window["request"] > 0 for window in windows),
        "exclusions": Counter(),
        "request_counts": Counter(),
        "replay_samples": 0,
        "selected_samples": 0,
        "independent_checks": {"packet_dump": [], "perf_memory": []},
    }
    address_evidence, address_inputs, address_audit = load_address_evidence(
        root, metadata
    )
    inputs.extend(address_inputs)
    audit["address_evidence"] = address_audit
    pcs: set[str] = set()
    seen_files = set()
    observed_cpus = set()
    declared_cpus = set()
    empty_cpus = set()
    with tempfile.TemporaryDirectory(prefix=".decode_", dir=output) as staging:
        provisional = Path(staging) / "selected.jsonl.gz"
        with gzip.open(provisional, "wt", compresslevel=3) as sink:
            for specification in metadata["perf_files"]:
                path = root / specification["path"]
                if path.resolve() in seen_files:
                    raise ValueError("Repeated raw perf input file")
                seen_files.add(path.resolve())
                identity = file_identity(path)
                inputs.append(identity)
                LOGGER.info(
                    "Indexing raw SPE file %s (%d bytes)", path.name, identity["size"]
                )
                perf = read_perf(
                    path, specification.get("cpu"), allow_empty=thread_capture
                )
                if thread_capture:
                    if not perf.audit["auxtrace_info_types"]:
                        raise ValueError(
                            "Thread-scoped capture requires ARM SPE AUXTRACE_INFO"
                        )
                    declared_cpus.update(metadata["cpus"])
                    empty_cpus.update(
                        set(metadata["cpus"]) - {key[0] for key in perf.streams}
                    )
                elif "cpu" in specification:
                    if specification["cpu"] not in metadata["cpus"]:
                        raise ValueError(
                            "Declared perf file CPU is outside capture CPU set"
                        )
                    declared_cpus.add(specification["cpu"])
                if thread_capture or not perf.streams:
                    reader = specification.get("reader", {})
                    if (
                        not isinstance(reader.get("pid"), int)
                        or reader["pid"] <= 0
                        or not str(reader.get("start_time", "")).isdigit()
                        or reader.get("alive_before_stop") is not True
                        or reader.get("completed") is not True
                        or reader.get("returncode") not in (0, 130, -2)
                    ):
                        raise ValueError(
                            "Empty or thread-scoped AUX capture requires "
                            "completed reader identity and flush evidence"
                        )
                    completion_path = root / metadata.get(
                        "capture_complete", "capture_complete.json"
                    )
                    completion = read_json(completion_path)
                    if completion.get("perf_readers_complete") is not True:
                        raise ValueError(
                            "Empty or thread-scoped AUX capture lacks "
                            "overall perf reader completion evidence"
                        )
                    inputs.append(file_identity(completion_path))
                    if not thread_capture:
                        empty_cpus.add(specification["cpu"])
                file_audit = {
                    **identity,
                    **perf.audit,
                    "decode": Counter(),
                    "streams": [],
                    "selected_records": 0,
                    "memory_not_comparable": Counter(),
                    "target_tid": specification.get("target_tid"),
                }
                expected: Counter[tuple[int, ...]] = Counter()
                memory_format = specification.get("memory_format", "perf_memory")
                next_progress = time.monotonic() + 30
                for key, chunks in perf.streams.items():
                    if key[0] not in metadata["cpus"]:
                        raise ValueError("AUXTRACE CPU is outside allowed capture set")
                    if thread_capture and key[2] != host_tid:
                        raise ValueError(
                            "Thread-scoped AUXTRACE TID differs from target"
                        )
                    observed_cpus.add(key[0])
                    file_audit["streams"].append(
                        {
                            "cpu": key[0],
                            "index": key[1],
                            "aux_tid": key[2],
                            "chunks": [
                                {
                                    "offset": str(c.offset),
                                    "file_offset": str(c.file_offset),
                                    "size": str(c.size),
                                    "reference": str(c.reference),
                                    "time_conversion": {
                                        name: str(value)
                                        for name, value in c.conversion.items()
                                    }
                                    if c.conversion is not None
                                    else None,
                                }
                                for c in chunks
                            ],
                        }
                    )
                    LOGGER.info(
                        "Decoding %s CPU=%d index=%d AUX_TID=%d bytes=%d",
                        path.name,
                        *key,
                        sum(chunk.size for chunk in chunks),
                    )
                    rows = (
                        scanner.decode(
                            perf,
                            chunks,
                            str(specification["path"]),
                            file_audit["decode"],
                            audit["exclusions"],
                            windows,
                            host_tid,
                        )
                        if scanner
                        else decode_stream(
                            perf,
                            chunks,
                            str(specification["path"]),
                            file_audit["decode"],
                            materialize=materialize_record,
                        )
                    )
                    for row in rows:
                        if file_audit["decode"]["records"] % 65536 == 0:
                            now = time.monotonic()
                            if now >= next_progress:
                                LOGGER.info(
                                    "SPE decode progress %s CPU=%d "
                                    "records=%d selected_so_far=%d",
                                    path.name,
                                    key[0],
                                    file_audit["decode"]["records"],
                                    file_audit["selected_records"],
                                )
                                next_progress = now + 30
                        window, reason = select_window(row, windows, starts, host_tid)
                        if window is None:
                            audit["exclusions"][reason] += 1
                            continue
                        ticks = int(row["ticks"])
                        row.update(
                            request=window["request"],
                            step=window["step"],
                            container_tid=window["container_tid"],
                            replay=window["replay_start"]
                            <= ticks
                            < window["replay_end"],
                        )
                        sink.write(json.dumps(row, separators=(",", ":")) + "\n")
                        pcs.add(row["pc"])
                        audit["selected_samples"] += 1
                        file_audit["selected_records"] += 1
                        audit["request_counts"][str(row["request"])] += 1
                        audit["replay_samples"] += row["replay"]
                        independent_key = memory_key(
                            row, memory_format in {"fields", "perf_memory_cpu"}
                        )
                        if independent_key is not None:
                            expected[independent_key] += 1
                        else:
                            missing = ",".join(
                                field
                                for field in ("va", "pa", "tot", "time_ns")
                                if row[field] is None
                            )
                            file_audit["memory_not_comparable"][missing] += 1
                            if (
                                specification.get("memory_text")
                                and row["time_ns"] is None
                            ):
                                raise ValueError(
                                    "Selected sample lacks usable TIME_CONV "
                                    "for independent perf check"
                                )
                for field, checker in (
                    ("packets_text", "packet_dump"),
                    ("memory_text", "perf_memory"),
                ):
                    if specification.get(field):
                        check_path = root / specification[field]
                        LOGGER.info(
                            "Starting independent %s check: %s",
                            checker,
                            check_path.name,
                        )
                        inputs.append(file_identity(check_path))
                        if field == "packets_text":
                            check = check_packet_dump(check_path, perf)
                        else:
                            check = check_memory_dump(
                                check_path, expected, memory_format
                            )
                            check["selected_not_comparable"] = dict(
                                file_audit["memory_not_comparable"]
                            )
                        audit["independent_checks"][checker].append(
                            {"file": str(specification["path"]), **check}
                        )
                    else:
                        audit["independent_checks"][checker].append(
                            {
                                "file": str(specification["path"]),
                                "status": "not_provided",
                            }
                        )
                audit["files"].append(file_audit)
                LOGGER.info(
                    "Decoded %s: %d records", path.name, file_audit["decode"]["records"]
                )
        if observed_cpus | empty_cpus != set(metadata["cpus"]):
            raise ValueError("Raw capture does not cover every declared CPU")
        audit.update(
            declared_cpus=sorted(declared_cpus),
            observed_aux_cpus=sorted(observed_cpus),
            empty_aux_cpus=sorted(empty_cpus),
            recording_scope="thread" if thread_capture else "cpu_or_legacy",
        )
        if not audit["selected_samples"]:
            raise ValueError(
                "No timestamped main-thread samples in formal fullgraph windows"
            )
        LOGGER.info("Resolving %d selected PCs against saved binaries", len(pcs))
        resolved, binaries, warnings = resolve_pcs(pcs, root, metadata)
        inputs.extend(binaries)
        audit["warnings"].extend(warnings)
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        samples_path = Path(staging) / "samples.jsonl.gz"
        LOGGER.info(
            "Enriching and aggregating %d selected samples", audit["selected_samples"]
        )
        with (
            gzip.open(provisional, "rt") as source,
            gzip.open(samples_path, "wt", compresslevel=6) as sink,
        ):
            for line in source:
                row = json.loads(line)
                row.update(resolved[row["pc"]])
                row.update(address_context(row, address_evidence))
                sink.write(json.dumps(row, separators=(",", ":")) + "\n")
                groups[row["pc_key"]].append(
                    {
                        field: value
                        for field, value in row.items()
                        if field not in ("raw_packets", "raw_hex", "counter_presence")
                    }
                )
        LOGGER.info("Building exact summaries for %d PC identities", len(groups))
        summaries = summarize(groups)
        audit.update(
            pc_count=len(summaries),
            raw_pc_count=len(pcs),
            resolution_counts=dict(
                Counter(row["resolution_status"] for row in resolved.values())
            ),
        )
        if sum(row["samples"] for row in summaries) != audit["selected_samples"]:
            raise ValueError("PC summary sample total mismatch")
        LOGGER.info("Rechecking identity of %d input files", len(inputs))
        for identity in inputs:
            if file_identity(Path(identity["path"])) != identity:
                raise ValueError("Capture input changed while decoding")
        for field, items in list(audit["independent_checks"].items()):
            statuses = {item["status"] for item in items}
            status = (
                "not_provided"
                if "not_provided" in statuses
                else ("pass" if "pass" in statuses else "not_applicable")
            )
            audit["independent_checks"][field] = {
                "status": status,
                "files": items,
            }
        audit["status"] = "pass"
        write_json(output / "pc_summary.json", summaries)
        write_json(
            output / "windows.json",
            [
                {
                    **row,
                    **{
                        key: str(row[key])
                        for key in ("start", "end", "replay_start", "replay_end")
                    },
                }
                for row in windows
            ],
        )
        samples_path.replace(output / "samples.jsonl.gz")
        write_json(output / "audit.json", audit)
    manifest = {
        "schema_version": 1,
        "status": "pass",
        "selected_samples": audit["selected_samples"],
        "pc_count": audit["pc_count"],
        "samples": "samples.jsonl.gz",
        "pc_summary": "pc_summary.json",
        "audit": "audit.json",
        "inputs": inputs,
        "scope": {
            "host_tid": host_tid,
            "container_tid": int(metadata.get("container_tid", host_tid)),
            "cpus": metadata["cpus"],
            "window_kind": "run_fullgraph",
            "formal_requests_only": True,
        },
        "semantics": {
            "timestamps": (
                "CNTVCT ticks; same capture markers; perf TIME_CONV per AUX chunk"
            ),
            "event_bits": (
                "Raw bits, co-occurring and non-additive; unknown bits preserved"
            ),
            "event_names": EVENT_NAMES,
            "counters": (
                "Independent sampled counters; absent values are null, not zero"
            ),
            "counter6": "Raw index 6; meaning unresolved",
            "cache_location": "unknown",
            "memory_policy": "unknown",
            "physical_resource": (
                "Observed PA membership in the selected GPU BAR0-5, "
                "then System RAM; otherwise unknown"
            ),
            "ram_numa": (
                "Observed System RAM address plus "
                "unique physical memory-block NUMA node"
            ),
            "data_mapping": (
                "VA membership in the capture maps snapshot; "
                "not access-time allocation or cache evidence"
            ),
            "samples": "Statistical CPU samples, not instruction counts or GPU latency",
            "pc_key": (
                "Snapshot SHA256 plus file offset; "
                "unresolved identities explicitly prefixed"
            ),
        },
    }
    write_json(output / "manifest.json", manifest)
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-library", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = normalize_run(
        args.run_dir, args.metadata, args.output, args.native_library
    )
    LOGGER.info(
        "Verified %d selected samples across %d PC identities",
        result["selected_samples"],
        result["pc_count"],
    )


if __name__ == "__main__":
    main()
