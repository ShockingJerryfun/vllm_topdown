# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Read live maps, NUMA location, PFNs and compound flags without page migration.

smaps KernelPageSize=4k does not disprove a 64k compound folio. This snapshot
therefore retains the PFNs and kpageflags; it does not claim CONT PTE visibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import struct
import subprocess
import time
from collections import Counter, defaultdict
from pathlib import Path

if __package__:
    from .evidence import complete_file_groups, mapping_kind, page_kind
else:
    from evidence import complete_file_groups, mapping_kind, page_kind


HEAD = 1 << 15
TAIL = 1 << 16
PAGE_SIZE = 4096
LOGGER = logging.getLogger(__name__)
ANONYMOUS_EXEC_MAPPING_LIMIT = 65536
ANONYMOUS_EXEC_TOTAL_LIMIT = 1048576


class MapsChangedError(ValueError):
    """The target changed its mappings while a read-only snapshot was collected."""


class PageChangedError(ValueError):
    """A code page moved while a quiescent snapshot was being collected."""


def word(fd: int, offset: int) -> int:
    raw = os.pread(fd, 8, offset)
    if len(raw) != 8:
        raise ValueError(f"Short pagemap/flags read at byte offset {offset}")
    return struct.unpack("=Q", raw)[0]


def read_resident_code_page(proc: Path, address: int, pfn: int) -> bytes:
    """Read an already observed resident code page with PFN checks on both sides."""
    with (proc / "pagemap").open("rb", buffering=0) as pagemap:
        before = word(pagemap.fileno(), address // PAGE_SIZE * 8)
        if not before & (1 << 63) or before & ((1 << 55) - 1) != pfn:
            raise ValueError("PFN is no longer resident/stable before code read")
        with (proc / "mem").open("rb", buffering=0) as memory:
            payload = os.pread(memory.fileno(), PAGE_SIZE, address)
        after = word(pagemap.fileno(), address // PAGE_SIZE * 8)
    if len(payload) != PAGE_SIZE:
        raise ValueError(f"Short anonymous executable read: {len(payload)} bytes")
    if not after & (1 << 63) or after & ((1 << 55) - 1) != pfn:
        raise ValueError("PFN changed while reading anonymous executable bytes")
    return payload


def capture_anonymous_executable(pid: int, destination: Path) -> dict:
    """Bounded diagnostic bytes from resident anonymous executable VMAs only."""
    with (destination / "mappings.csv").open() as stream:
        mappings = [
            row
            for row in csv.DictReader(stream)
            if row["scope"] == "ordinary"
            and "x" in row["perms"]
            and int(row["inode"]) == 0
            and mapping_kind(row["path"], row["perms"])
            in ("anonymous_mapping", "heap_mapping")
        ]
    selected = {int(row["begin"], 16): row for row in mappings}
    details: dict[int, list[dict]] = defaultdict(list)
    with (destination / "page_evidence.csv").open() as stream:
        for row in csv.DictReader(stream):
            begin = int(row["mapping_begin"], 16)
            if begin in selected:
                details[begin].append(row)
    evidence: dict = {
        "schema_version": 1,
        "pid": pid,
        "selection": (
            "ordinary inode-zero anonymous/heap mappings with "
            "executable permission; resident pages only"
        ),
        "per_mapping_limit_bytes": ANONYMOUS_EXEC_MAPPING_LIMIT,
        "total_limit_bytes": ANONYMOUS_EXEC_TOTAL_LIMIT,
        "read_effect": (
            "read-only /proc/PID/mem access can touch already "
            "resident code; no data/nonresident pages deliberately read"
        ),
        "origin": (
            "unknown; bytes/hash do not establish allocator, library "
            "or executed instructions"
        ),
        "mappings": [],
        "captured_bytes": 0,
    }
    output = destination / "anonymous_exec"
    if mappings:
        output.mkdir(mode=0o700)
    attempted = 0
    for mapping in mappings:
        begin, end = int(mapping["begin"], 16), int(mapping["end"], 16)
        rows = details[begin]
        record = {
            "begin": hex(begin),
            "end": hex(end),
            "perms": mapping["perms"],
            "path": mapping["path"],
            "inode": int(mapping["inode"]),
            "mapped_pages": int(mapping["pages"]),
            "resident_pages": int(mapping["present"]),
            "not_resident_pages": int(mapping["pages"]) - int(mapping["present"]),
            "numa_counts": {
                key: int(mapping[key]) for key in ("N0", "N1", "N2", "N3", "errors")
            },
            "backing_counts": dict(Counter(row["page_kind"] for row in rows)),
            "pages": [],
            "status": "unknown",
            "captured_bytes": 0,
        }
        mapping_attempted = 0
        for row in rows:
            page = {
                key: row[key]
                for key in (
                    "address",
                    "pfn",
                    "node",
                    "kpageflags",
                    "page_kind",
                    "pfn_recheck_same",
                )
            }
            page.update(status="unknown", reason="", file=None, sha256=None)
            record["pages"].append(page)
            if (
                row["error"]
                or row["pfn_recheck_same"] != "True"
                or int(row["pfn"], 16) == 0
            ):
                page["reason"] = "unavailable_or_unstable_resident_page_evidence"
                continue
            if (
                mapping_attempted + PAGE_SIZE > ANONYMOUS_EXEC_MAPPING_LIMIT
                or attempted + PAGE_SIZE > ANONYMOUS_EXEC_TOTAL_LIMIT
            ):
                page["reason"] = "diagnostic_byte_limit"
                continue
            mapping_attempted += PAGE_SIZE
            attempted += PAGE_SIZE
            address, pfn = int(row["address"], 16), int(row["pfn"], 16)
            try:
                payload = read_resident_code_page(
                    Path("/proc") / str(pid), address, pfn
                )
            except (OSError, ValueError) as exc:
                page["reason"] = str(exc)
                continue
            relative = f"anonymous_exec/{begin:x}_{address:x}.bin"
            with (destination / relative).open("xb") as stream:
                stream.write(payload)
            page.update(
                status="captured",
                file=relative,
                sha256=hashlib.sha256(payload).hexdigest(),
                byte_count=len(payload),
            )
            record["captured_bytes"] += len(payload)
            evidence["captured_bytes"] += len(payload)
        record["captured_pages"] = sum(
            page["status"] == "captured" for page in record["pages"]
        )
        record["unknown_resident_pages"] = (
            record["resident_pages"] - record["captured_pages"]
        )
        if record["resident_pages"] and not record["unknown_resident_pages"]:
            record["status"] = "captured_resident_pages"
        evidence["mappings"].append(record)
    (destination / "anonymous_exec.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    return evidence


def annotate_pages(pid: int, destination: Path) -> dict:
    """Retain per-page backing evidence, including mixed file/COW and shared RAM."""
    with (destination / "mappings.csv").open() as stream:
        mappings = list(csv.DictReader(stream))
    with (destination / "pages.csv").open() as stream:
        pages = {int(row["address"], 16): row for row in csv.DictReader(stream)}
    kinds = Counter()
    unknown = unstable = 0
    columns = [
        "address",
        "pfn",
        "node",
        "mapping_begin",
        "path",
        "perms",
        "file_offset",
        "kpageflags",
        "pagemap_entry",
        "mapcount",
        "page_kind",
        "mapping_kind",
        "file_or_shared_anon",
        "exclusive",
        "pfn_recheck_same",
        "error",
    ]
    with (
        Path("/proc/kpageflags").open("rb", buffering=0) as flags_file,
        Path("/proc/kpagecount").open("rb", buffering=0) as count_file,
        (Path("/proc") / str(pid) / "pagemap").open("rb", buffering=0) as pagemap,
        (destination / "page_evidence.csv").open("x") as output,
    ):
        writer = csv.DictWriter(output, fieldnames=columns)
        writer.writeheader()
        for mapping in mappings:
            if mapping["scope"] != "ordinary":
                continue
            begin, end, offset = (
                int(mapping[key], 16) for key in ("begin", "end", "offset")
            )
            for address in range(begin, end, PAGE_SIZE):
                if address not in pages:
                    continue
                raw = pages[address]
                pfn = int(raw["pfn"], 16)
                row = {
                    "address": hex(address),
                    "pfn": hex(pfn),
                    "node": raw["node"],
                    "mapping_begin": hex(begin),
                    "path": mapping["path"],
                    "perms": mapping["perms"],
                    "file_offset": hex(offset + address - begin),
                    "mapping_kind": mapping_kind(mapping["path"], mapping["perms"]),
                    "page_kind": "unknown_backing",
                    "error": "",
                    "pfn_recheck_same": False,
                }
                if pfn == 0:
                    row["error"] = "PFN zero/redacted"
                else:
                    flags = word(flags_file.fileno(), pfn * 8)
                    entry = word(pagemap.fileno(), address // PAGE_SIZE * 8)
                    same = bool(entry & (1 << 63)) and entry & ((1 << 55) - 1) == pfn
                    row.update(
                        kpageflags=hex(flags),
                        pagemap_entry=hex(entry),
                        mapcount=word(count_file.fileno(), pfn * 8),
                        file_or_shared_anon=bool(entry & (1 << 61)),
                        exclusive=bool(entry & (1 << 56)),
                        pfn_recheck_same=same,
                        page_kind=page_kind(
                            mapping["path"], mapping["perms"], flags, entry
                        ),
                    )
                    unstable += int(not same)
                unknown += int(row["page_kind"] == "unknown_backing")
                kinds[row["page_kind"]] += 1
                writer.writerow(row)
    return {
        "backing_page_counts": dict(kinds),
        "unknown_backing_pages": unknown,
        "unstable_during_query_pages": unstable,
        "sharing_boundary": "mapcount counts mappings, not distinct owner processes",
    }


def folio_descriptor(
    pfn: int,
    descriptor_cache: dict[int, tuple[int, int]],
    flags_cache: dict[int, int],
    descriptor: int,
) -> tuple[int, int]:
    """Read compound head/tail extent; unknown or >16MiB folios remain unknown."""
    if pfn in descriptor_cache:
        return descriptor_cache[pfn]

    def flags_at(number: int) -> int:
        if number not in flags_cache:
            flags_cache[number] = word(descriptor, number * 8)
        return flags_cache[number]

    if not flags_at(pfn) & (HEAD | TAIL):
        descriptor_cache[pfn] = (pfn, 1)
        return pfn, 1
    head = pfn
    while flags_at(head) & TAIL and pfn - head < 4096 and head:
        head -= 1
    if not flags_at(head) & HEAD or flags_at(head) & TAIL:
        return 0, 0
    following = head + 1
    while following - head <= 4096 and flags_at(following) & TAIL:
        following += 1
    count = following - head
    if count > 4096 or count < 2 or count & (count - 1) or not head <= pfn < following:
        return 0, 0
    for number in range(head, following):
        descriptor_cache[number] = (head, count)
    return head, count


def inspect_code(
    pid: int, destination: Path, identity_path: Path, expected_node: int
) -> dict:
    """Summarize every observed executable mapping and preserve per-page evidence."""
    identity = json.loads(identity_path.read_text())
    copied = {record["path"]: record for record in identity["files"]}
    with (destination / "mappings.csv").open() as stream:
        mappings = list(csv.DictReader(stream))
    with (destination / "pages.csv").open() as stream:
        pages = {
            int(row["address"], 16): (int(row["pfn"], 16), int(row["node"]))
            for row in csv.DictReader(stream)
        }
    code = defaultdict(Counter)
    categories = defaultdict(Counter)
    issues = []
    with (
        Path("/proc/kpageflags").open("rb", buffering=0) as flags_file,
        (Path("/proc") / str(pid) / "pagemap").open("rb", buffering=0) as pagemap,
        (destination / "code_pages.csv").open("x") as page_output,
        (destination / "groups_64k.csv").open("x") as group_output,
    ):
        writer = csv.writer(page_output)
        writer.writerow(
            [
                "path",
                "address",
                "file_offset",
                "pfn",
                "node",
                "kpageflags",
                "folio_head_pfn",
                "folio_pages",
                "folio_head_flags",
                "folio_after_flags",
            ]
        )
        groups = csv.writer(group_output)
        groups.writerow(
            [
                "path",
                "address",
                "file_offset",
                "present",
                "expected_node_pages",
                "contiguous",
                "aligned",
                "compound_at_least_64k",
                "exact_64k",
                "next_pfn_kpageflags",
            ]
        )
        descriptor_cache = {}
        flags_cache = {}
        for mapping in mappings:
            if mapping["scope"] != "ordinary":
                continue
            path = mapping["path"]
            executable = "x" in mapping["perms"]
            category = "code" if executable else mapping_kind(path, mapping["perms"])
            categories[category].update(
                {
                    key: int(mapping[key])
                    for key in ("present", "N0", "N1", "N2", "N3", "errors")
                }
            )
            if not executable:
                continue
            begin, end, offset = (
                int(mapping[key], 16) for key in ("begin", "end", "offset")
            )
            if path.startswith("/"):
                record = copied.get(path)
                if record is None:
                    issues.append(f"Unregistered executable file: {path}")
                else:
                    major, minor = (
                        int(part, 16) for part in mapping["device"].split(":")
                    )
                    actual = (major, minor, int(mapping["inode"]))
                    expected = (
                        os.major(record["copy_device"]),
                        os.minor(record["copy_device"]),
                        record["copy_inode"],
                    )
                    if actual != expected:
                        issues.append(
                            f"Executable mapping is not frozen task copy: {path}"
                        )
            flags = {}
            for address in range(begin, end, PAGE_SIZE):
                if address not in pages:
                    continue
                pfn, node = pages[address]
                if pfn == 0:
                    raise PermissionError(
                        "PFN zero/redacted; host CAP_SYS_ADMIN is required"
                    )
                flag = word(flags_file.fileno(), pfn * 8)
                flags_cache[pfn] = flag
                head_pfn, folio_pages = folio_descriptor(
                    pfn, descriptor_cache, flags_cache, flags_file.fileno()
                )
                entry = word(pagemap.fileno(), address // PAGE_SIZE * 8)
                if not entry & (1 << 63) or entry & ((1 << 55) - 1) != pfn:
                    raise PageChangedError(
                        f"Code PFN changed during query: {path} {address:x}"
                    )
                flags[address] = flag
                code[path]["present_pages"] += 1
                code[path]["expected_node_pages"] += int(node == expected_node)
                code[path]["query_errors"] += int(node < 0)
                code[path][
                    "compound_pages" if flag & (HEAD | TAIL) else "base_pages"
                ] += 1
                writer.writerow(
                    [
                        path,
                        hex(address),
                        hex(offset + address - begin),
                        hex(pfn),
                        node,
                        hex(flag),
                        hex(head_pfn),
                        folio_pages,
                        hex(flags_cache[head_pfn]) if folio_pages else "",
                        hex(flags_cache[head_pfn + folio_pages])
                        if folio_pages > 1
                        else "",
                    ]
                )
            for group in complete_file_groups(begin, end, offset):
                addresses = list(range(group, group + 65536, PAGE_SIZE))
                present = [address for address in addresses if address in flags]
                full = len(present) == 16
                contiguous = full and all(
                    pages[address][0] == pages[group][0] + index
                    for index, address in enumerate(addresses)
                )
                aligned = full and pages[group][0] % 16 == 0
                compound = bool(
                    contiguous
                    and aligned
                    and flags[group] & (HEAD | TAIL)
                    and all(flags[address] & TAIL for address in addresses[1:])
                )
                following_flag = (
                    word(flags_file.fileno(), (pages[group][0] + 16) * 8)
                    if full
                    else None
                )
                exact = bool(
                    compound and flags[group] & HEAD and not following_flag & TAIL
                )
                local = sum(pages[address][1] == expected_node for address in present)
                code[path]["full_groups" if full else "partial_groups"] += 1
                code[path]["compound_at_least_64k_groups"] += int(compound)
                code[path]["exact_64k_groups"] += int(exact)
                groups.writerow(
                    [
                        path,
                        hex(group),
                        hex(offset + group - begin),
                        len(present),
                        local,
                        contiguous,
                        aligned,
                        compound,
                        exact,
                        "" if following_flag is None else hex(following_flag),
                    ]
                )
    return {
        "pid": pid,
        "expected_node": expected_node,
        "categories": dict(categories),
        "code_files": dict(code),
        "mapping_identity_issues": sorted(set(issues)),
        "all_code_pfns_stable_during_query": True,
        "contiguous_pte_bits_observed": False,
        "kernel_base_page_bytes": PAGE_SIZE,
        "all_ordinary_code_on_expected_node": all(
            values["present_pages"] == values["expected_node_pages"]
            for values in code.values()
        ),
        "all_observed_code_4k_base_pages": bool(code)
        and all(values["compound_pages"] == 0 for values in code.values()),
    }


def capture(
    pid: int,
    destination: Path,
    identity: Path,
    node: int,
    query: Path,
    original_namespace: str | None = None,
) -> dict:
    if os.sysconf("SC_PAGE_SIZE") != PAGE_SIZE:
        raise ValueError("Expected 4KB kernel base pages")
    proc = Path("/proc") / str(pid)
    destination.mkdir(parents=True, exist_ok=False)
    for name in ("status", "maps", "numa_maps", "smaps", "stat", "cgroup", "mountinfo"):
        (destination / name).write_bytes((proc / name).read_bytes())
    subprocess.run(
        [
            str(query),
            str(pid),
            str(destination / "mappings.csv"),
            str(destination / "pages.csv"),
        ],
        check=True,
    )
    page_summary = annotate_pages(pid, destination)
    anonymous = capture_anonymous_executable(pid, destination)
    summary = inspect_code(pid, destination, identity, node)
    summary.update(page_summary)
    summary["snapshot_schema_version"] = 2
    summary["identity_sha256"] = hashlib.sha256(identity.read_bytes()).hexdigest()
    summary["pid_start_ticks"] = (
        (destination / "stat").read_text().rsplit(")", 1)[1].split()[19]
    )
    summary["mount_namespace"] = os.readlink(proc / "ns/mnt")
    summary["observer_mount_namespace"] = os.readlink("/proc/self/ns/mnt")
    original = original_namespace or os.environ.get("CODE_PARENT_MOUNT_NAMESPACE")
    if original and not re.fullmatch(r"mnt:\[\d+\]", original):
        raise ValueError(
            "Original mount namespace must use /proc/PID/ns/mnt link format"
        )
    summary["original_mount_namespace"] = original
    summary["original_mount_namespace_source"] = (
        "argument"
        if original_namespace
        else "CODE_PARENT_MOUNT_NAMESPACE"
        if original
        else "unavailable"
    )
    summary["anonymous_executable_mappings"] = len(anonymous["mappings"])
    summary["anonymous_executable_captured_bytes"] = anonymous["captured_bytes"]
    final_maps = (proc / "maps").read_bytes()
    if final_maps != (destination / "maps").read_bytes():
        (destination / "maps_changed").write_bytes(final_maps)
        raise MapsChangedError(
            "Maps changed during page snapshot; capture at a quiescent gate"
        )
    if (proc / "stat").read_text().rsplit(")", 1)[1].split()[19] != summary[
        "pid_start_ticks"
    ]:
        raise ValueError("Process identity changed during page snapshot")
    (destination / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


def capture_stable(
    pid: int,
    destination: Path,
    identity: Path,
    node: int,
    query: Path,
    original_namespace: str | None = None,
) -> dict:
    for attempt in range(3):
        try:
            return capture(pid, destination, identity, node, query, original_namespace)
        except (MapsChangedError, PageChangedError):
            if attempt == 2:
                raise
        retained = destination.with_name(f"{destination.name}.retry{attempt + 1}")
        if retained.exists():
            raise FileExistsError(f"Preserved snapshot already exists: {retained}")
        destination.rename(retained)
        LOGGER.info(
            "Page mappings changed; preserved %s and retrying snapshot", retained
        )
        time.sleep(1)
    raise RuntimeError("Snapshot attempts exhausted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--node", type=int, choices=range(4), required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--original-namespace",
        help=(
            "Verified original container mount namespace; defaults to "
            "CODE_PARENT_MOUNT_NAMESPACE"
        ),
    )
    parser.add_argument(
        "--query", type=Path, default=Path(__file__).with_name("process_pages")
    )
    args = parser.parse_args()
    summary = capture_stable(
        args.pid,
        args.output,
        args.identity,
        args.node,
        args.query,
        args.original_namespace,
    )
    LOGGER.info(
        "Snapshot: code_local=%s copy_identity_issues=%s",
        summary["all_ordinary_code_on_expected_node"],
        summary["mapping_identity_issues"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
