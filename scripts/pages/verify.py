# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Offline, fail-closed acceptance of one before/after CPU page snapshot pair.

Only saved evidence is read. No target files are opened and no page is migrated.
Pass means ordinary resident CPU mappings satisfy the stated rules; excluded
device/kernel mappings retain unknown residency and NUMA location explicitly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

if __package__:
    from .closure import classify_closure_page
    from .evidence import (
        EXCLUSIVE,
        FILE_OR_SHARED_ANON,
        HEAD,
        PAGE_SIZE,
        PRESENT,
        TAIL,
        complete_file_groups,
        device_parts,
        mapping_kind,
        page_kind,
    )
else:
    from closure import classify_closure_page
    from evidence import (
        EXCLUSIVE,
        FILE_OR_SHARED_ANON,
        HEAD,
        PAGE_SIZE,
        PRESENT,
        TAIL,
        complete_file_groups,
        device_parts,
        mapping_kind,
        page_kind,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def hexadecimal(value: str) -> int:
    return int(value, 16)


def boolean(value: str) -> bool:
    if value not in ("True", "False"):
        raise ValueError(f"Invalid saved Boolean: {value!r}")
    return value == "True"


def fraction(numerator: int, denominator: int) -> dict:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "fraction": numerator / denominator if denominator else None,
    }


def evidence_hashes(folder: Path) -> dict[str, str]:
    names = (
        "summary.json",
        "maps",
        "stat",
        "status",
        "smaps",
        "numa_maps",
        "cgroup",
        "mountinfo",
        "mappings.csv",
        "pages.csv",
        "page_evidence.csv",
        "code_pages.csv",
        "groups_64k.csv",
        "anonymous_exec.json",
    )
    result = {}
    names = (
        *names,
        *(
            str(path.relative_to(folder))
            for path in sorted((folder / "anonymous_exec").glob("*.bin"))
        ),
    )
    for name in names:
        path = folder / name
        if path.is_file():
            with path.open("rb") as stream:
                result[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return result


class Findings:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict] = {}

    def add(
        self, phase: str, code: str, message: str, example: str = "", count: int = 1
    ) -> None:
        item = self.items.setdefault(
            (phase, code),
            {
                "phase": phase,
                "code": code,
                "message": message,
                "count": 0,
                "examples": [],
            },
        )
        item["count"] += count
        if example and example not in item["examples"] and len(item["examples"]) < 8:
            item["examples"].append(example)

    def rows(self) -> list[dict]:
        return list(self.items.values())


def indexed(rows: list[dict], key: str, base: int = 16) -> dict[int, dict]:
    result = {}
    for row in rows:
        value = int(row[key], base)
        if value in result:
            raise ValueError(f"Duplicate {key}: {row[key]}")
        result[value] = row
    return result


def mapping_key(row: dict) -> tuple:
    return (
        hexadecimal(row["begin"]),
        hexadecimal(row["end"]),
        row["perms"],
        hexadecimal(row["offset"]),
        row["device"],
        int(row["inode"]),
        row["path"],
    )


def read_maps(path: Path) -> set[tuple]:
    result = set()
    for line in path.read_text().splitlines():
        fields = line.split(maxsplit=5)
        begin, end = (int(value, 16) for value in fields[0].split("-"))
        result.add(
            (
                begin,
                end,
                fields[1],
                int(fields[2], 16),
                fields[3],
                int(fields[4]),
                fields[5] if len(fields) == 6 else "",
            )
        )
    return result


def guarded_code_metadata(
    manifest: dict, files: dict, findings: Findings
) -> dict | None:
    """An optional guard declaration must name its ordinary frozen ELF copy."""
    if "code_page_guard" not in manifest:
        return None
    guard = manifest["code_page_guard"]
    if not isinstance(guard, dict):
        findings.add(
            "condition",
            "invalid_code_page_guard",
            "Guard declaration must be an object",
        )
        return None
    path, digest = guard.get("path"), guard.get("sha256")
    record = files.get(path) if isinstance(path, str) else None
    if (
        guard.get("method") != "executable_vma_madv_nohugepage"
        or not isinstance(path, str)
        or not path.startswith("/")
        or not isinstance(digest, str)
        or not re.fullmatch("[0-9a-f]{64}", digest)
        or record is None
        or record.get("sha256") != digest
        or record.get("task_generated_diagnostic", False)
    ):
        findings.add(
            "condition",
            "invalid_code_page_guard",
            (
                "Guard path/method/hash must match an ordinary copied ELF"
                " in the condition identity"
            ),
        )
        return None
    return {"path": path, "sha256": digest, "method": guard["method"]}


def smaps_vm_flags(path: Path) -> dict[tuple, set[str]]:
    """Bind each VmFlags record to the complete original maps identity."""
    result = {}
    current = None
    for line in path.read_text().splitlines():
        if re.match(r"^[0-9a-f]+-[0-9a-f]+ ", line):
            fields = line.split(maxsplit=5)
            begin, end = (int(value, 16) for value in fields[0].split("-"))
            current = (
                begin,
                end,
                fields[1],
                int(fields[2], 16),
                fields[3],
                int(fields[4]),
                fields[5] if len(fields) == 6 else "",
            )
        elif line.startswith("VmFlags:"):
            if current is None or current in result:
                raise ValueError("Orphan or duplicate VmFlags in smaps")
            result[current] = set(line.split()[1:])
    return result


def verify_code_guard(
    folder: Path,
    mappings: list[dict],
    guard: dict | None,
    phase: str,
    findings: Findings,
) -> dict:
    if guard is None:
        return {"declared": False}
    flags = smaps_vm_flags(folder / "smaps")
    executable = [
        row
        for row in mappings
        if row["scope"] == "ordinary"
        and "x" in row["perms"]
        and int(row["inode"]) > 0
        and mapping_kind(row["path"], row["perms"]) == "file_mapping"
    ]
    loaded = any(row["path"] == guard["path"] for row in executable)
    if not loaded:
        findings.add(
            phase,
            "code_page_guard_not_loaded",
            "Declared guard ELF has no actual executable mapping",
            guard["path"],
        )
    protected = 0
    for mapping in executable:
        observed = flags.get(mapping_key(mapping))
        if observed is None:
            findings.add(
                phase,
                "code_guard_smaps_missing",
                "Executable mapping has no matching raw smaps/VmFlags evidence",
                mapping["path"],
            )
        elif "nh" not in observed or "hg" in observed:
            findings.add(
                phase,
                "code_guard_nh_missing",
                (
                    "Guarded condition requires nh and no hg on every "
                    "ordinary file executable VMA"
                ),
                mapping["path"],
            )
        else:
            protected += 1
    return {
        **guard,
        "declared": True,
        "loaded": loaded,
        "ordinary_file_executable_mappings": len(executable),
        "nh_mappings": protected,
        "scope": "file executable VMAs only; does not establish physical folio size",
    }


def code_identity(mapping: dict, files: dict, phase: str, findings: Findings) -> None:
    path = mapping["path"]
    if not path.startswith("/"):
        findings.add(
            phase,
            "anonymous_executable",
            "Executable mapping has no frozen ELF identity",
            path or "[anonymous]",
        )
        return
    record = files.get(path)
    if record is None:
        findings.add(
            phase,
            "unregistered_executable",
            "Ordinary executable file is absent from condition identity",
            path,
        )
        return
    actual = tuple(int(value, 16) for value in mapping["device"].split(":")) + (
        int(mapping["inode"]),
    )
    expected = device_parts(int(record["copy_device"])) + (int(record["copy_inode"]),)
    if actual != expected:
        findings.add(
            phase,
            "code_inode_mismatch",
            "Loaded executable is not the frozen condition inode",
            path,
        )
    if not record.get("task_generated_diagnostic", False):
        original = (record.get("source_device"), record.get("source_inode"))
        if None in original or original == (
            record["copy_device"],
            record["copy_inode"],
        ):
            findings.add(
                phase,
                "copy_not_independent",
                "Ordinary code copy has no independent source/copy inode proof",
                path,
            )
    digest = record.get("sha256", "")
    if len(digest) != 64 or any(
        character not in "0123456789abcdef" for character in digest
    ):
        findings.add(
            phase,
            "missing_binary_hash",
            "Condition identity lacks a valid frozen SHA256",
            path,
        )


def folio_pages(row: dict) -> int:
    """Validate saved head/extent evidence rather than accepting a condition label."""
    flags, pfn = hexadecimal(row["kpageflags"]), hexadecimal(row["pfn"])
    count, head = int(row["folio_pages"]), hexadecimal(row["folio_head_pfn"])
    head_flags = hexadecimal(row["folio_head_flags"])
    if count == 1:
        if flags & (HEAD | TAIL) or head != pfn or head_flags & (HEAD | TAIL):
            raise ValueError("Base-page descriptor contradicts compound flags")
        return count
    if count < 2 or count & (count - 1) or not head <= pfn < head + count:
        raise ValueError("Unknown or invalid compound extent")
    if head % count or not head_flags & HEAD or head_flags & TAIL:
        raise ValueError("Invalid compound head")
    if not flags & (HEAD if pfn == head else TAIL):
        raise ValueError("Page flags disagree with compound position")
    if hexadecimal(row["folio_after_flags"]) & TAIL:
        raise ValueError("Claimed compound extent ends inside the same folio")
    return count


def runtime_closures(
    folder: Path,
    mappings: list[dict],
    files: dict,
    pages: dict,
    details: dict,
    code: dict,
    pid: int,
    node: int,
    phase: str,
    findings: Findings,
) -> dict[int, dict]:
    """A single structurally identified runtime page; all other anonymous code fails."""
    anonymous = [
        row
        for row in mappings
        if row["scope"] == "ordinary"
        and "x" in row["perms"]
        and not row["path"].startswith("/")
    ]
    if not anonymous:
        return {}
    try:
        if len(anonymous) != 1:
            raise ValueError(
                "Runtime closure exception permits at most one anonymous executable VMA"
            )
        mapping = anonymous[0]
        begin, end = int(mapping["begin"], 16), int(mapping["end"], 16)
        if (
            end - begin != PAGE_SIZE
            or mapping["perms"] != "rwxp"
            or int(mapping["inode"]) != 0
            or mapping["path"]
        ):
            raise ValueError(
                "Runtime closure requires one unnamed private RWX base-page mapping"
            )
        if int(mapping["present"]) != 1 or folio_pages(code[begin]) != 1:
            raise ValueError(
                "Runtime closure page must be resident with actual "
                "noncompound 4KB backing"
            )
        raw, backing = pages[begin], details[begin]
        if (
            int(raw["node"]) != node
            or backing["page_kind"] != "anonymous"
            or backing["pfn_recheck_same"] != "True"
        ):
            raise ValueError(
                "Runtime closure page lacks stable anonymous CPU-local evidence"
            )
        dump = json.loads((folder / "anonymous_exec.json").read_text())
        if dump["pid"] != pid or len(dump["mappings"]) != 1:
            raise ValueError("Anonymous byte evidence has a different PID or scope")
        record = dump["mappings"][0]
        if (
            int(record["begin"], 16),
            int(record["end"], 16),
            record["perms"],
            record["path"],
            record["inode"],
        ) != (begin, end, "rwxp", "", 0):
            raise ValueError("Anonymous byte evidence disagrees with actual mapping")
        if (
            record["status"] != "captured_resident_pages"
            or record["captured_bytes"] != PAGE_SIZE
            or len(record["pages"]) != 1
        ):
            raise ValueError("Anonymous executable byte evidence is incomplete")
        saved = record["pages"][0]
        if (
            int(saved["address"], 16),
            int(saved["pfn"], 16),
            int(saved["node"]),
            saved["pfn_recheck_same"],
            saved["status"],
            saved["byte_count"],
        ) != (begin, int(raw["pfn"], 16), node, "True", "captured", PAGE_SIZE):
            raise ValueError(
                "Captured closure bytes are not bound to this resident PFN/node"
            )
        expected_file = f"anonymous_exec/{begin:x}_{begin:x}.bin"
        if saved["file"] != expected_file:
            raise ValueError("Unexpected closure byte evidence path")
        payload = (folder / expected_file).read_bytes()
        if hashlib.sha256(payload).hexdigest() != saved["sha256"]:
            raise ValueError("Closure byte evidence SHA256 mismatch")
        classified = classify_closure_page(payload, mappings, files)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        findings.add(
            phase,
            "runtime_closure_unproven",
            "Anonymous executable does not meet the narrow runtime closure exception",
            str(exc),
        )
        return {}
    classified.update(
        address=hex(begin),
        pfn=hex(int(raw["pfn"], 16)),
        node=node,
        dump_file=expected_file,
        perms="rwxp",
        counted_as="one ordinary 4KB code page; never exact64KB coverage",
    )
    return {begin: classified}


def verify_phase(
    folder: Path,
    files: dict,
    identity_hash: str,
    mode: str,
    node: int,
    phase: str,
    findings: Findings,
    guard: dict | None = None,
) -> dict:
    summary = json.loads((folder / "summary.json").read_text())
    if summary.get("snapshot_schema_version") != 2:
        findings.add(
            phase,
            "old_snapshot_schema",
            "Fresh schema-2 per-page backing/extent evidence is required",
        )
    if summary.get("identity_sha256") != identity_hash:
        findings.add(
            phase,
            "identity_manifest_mismatch",
            "Snapshot was not captured against this exact condition identity",
        )
    if (
        summary.get("kernel_base_page_bytes") != PAGE_SIZE
        or summary.get("expected_node") != node
    ):
        findings.add(
            phase,
            "snapshot_condition_mismatch",
            "Snapshot base-page size or requested node differs",
        )
    stat = (folder / "stat").read_text()
    pid = int(stat.split(" ", 1)[0])
    start = int(stat.rsplit(")", 1)[1].split()[19])
    if summary.get("pid") != pid or int(summary.get("pid_start_ticks", -1)) != start:
        findings.add(
            phase,
            "process_identity_inconsistent",
            "Saved PID/start identity is inconsistent",
        )
    for name in ("status", "smaps", "numa_maps", "cgroup", "mountinfo"):
        if not (folder / name).is_file():
            findings.add(
                phase,
                "missing_raw_evidence",
                "Required raw process evidence is missing",
                name,
            )
    original = summary.get("original_mount_namespace")
    if not isinstance(original, str) or not re.fullmatch(r"mnt:\[\d+\]", original):
        findings.add(
            phase,
            "original_namespace_unknown",
            "Verified original container mount namespace is missing or malformed",
        )
    if not summary.get("mount_namespace") or summary["mount_namespace"] == original:
        findings.add(
            phase,
            "namespace_not_isolated",
            (
                "Worker mount namespace is not distinct from the original"
                " container namespace"
            ),
        )
    mappings = read_csv(folder / "mappings.csv")
    guard_evidence = verify_code_guard(folder, mappings, guard, phase, findings)
    if {mapping_key(row) for row in mappings} != read_maps(folder / "maps"):
        findings.add(phase, "maps_disagree", "Raw maps and queried mappings disagree")
    pages = indexed(read_csv(folder / "pages.csv"), "address")
    details = indexed(read_csv(folder / "page_evidence.csv"), "address")
    code = indexed(read_csv(folder / "code_pages.csv"), "address")
    groups = indexed(read_csv(folder / "groups_64k.csv"), "address")
    closures = runtime_closures(
        folder, mappings, files, pages, details, code, pid, node, phase, findings
    )
    if set(details) != set(pages):
        findings.add(
            phase,
            "page_evidence_incomplete",
            "Per-page backing evidence does not cover the resident query",
            count=len(set(details) ^ set(pages)),
        )
    totals = Counter()
    backing = Counter()
    sharing = Counter()
    excluded: dict[str, dict] = {}
    file_coverage = {}
    seen = set()
    code_seen = set()
    group_seen = set()
    signatures = {}
    data_pfns = {}
    code_maps = set()
    all_pfns = set()
    for mapping in mappings:
        begin, end, offset = (
            hexadecimal(mapping[key]) for key in ("begin", "end", "offset")
        )
        if (
            begin >= end
            or begin % PAGE_SIZE
            or end % PAGE_SIZE
            or int(mapping["pages"]) != (end - begin) // PAGE_SIZE
        ):
            raise ValueError("Invalid mapping range/page count")
        kind = mapping_kind(mapping["path"], mapping["perms"])
        if mapping["scope"] != "ordinary":
            item = excluded.setdefault(
                kind,
                {
                    "mappings": 0,
                    "mapped_pages": 0,
                    "resident_pages": None,
                    "numa_location": "not_queried",
                    "paths": [],
                },
            )
            item["mappings"] += 1
            item["mapped_pages"] += int(mapping["pages"])
            if mapping["path"] not in item["paths"] and len(item["paths"]) < 8:
                item["paths"].append(mapping["path"])
            if kind == "shared_ram_mapping":
                findings.add(
                    phase,
                    "shared_ram_excluded",
                    "/dev/shm and /dev/zero RAM must be queried",
                    mapping["path"],
                )
            if (
                kind not in ("device_mapping", "kernel_special_mapping")
                and mapping["perms"][:3] != "---"
            ):
                findings.add(
                    phase,
                    "unexplained_exclusion",
                    "Accessible ordinary mapping was excluded",
                    mapping["path"],
                )
            continue
        executable = "x" in mapping["perms"]
        domain = "code" if executable else "data"
        totals[f"{domain}_mapped_pages"] += int(mapping["pages"])
        mapping_nodes = Counter()
        resident = [
            address for address in range(begin, end, PAGE_SIZE) if address in pages
        ]
        if len(resident) != int(mapping["present"]):
            findings.add(
                phase,
                "resident_count_mismatch",
                "Mapping resident count disagrees with page rows",
                hex(begin),
            )
        if executable:
            code_maps.add(mapping_key(mapping))
            if files.get(mapping["path"], {}).get("data_only") is True:
                findings.add(
                    phase,
                    "data_copy_executable",
                    "A declared data-only copy has an executable mapping",
                    mapping["path"],
                )
            if begin not in closures:
                code_identity(mapping, files, phase, findings)
        coverage = (
            file_coverage.setdefault(mapping["path"] or "[anonymous]", Counter())
            if executable
            else Counter()
        )
        complete_starts = list(complete_file_groups(begin, end, offset))
        interior = {
            address
            for group in complete_starts
            for address in range(group, group + 65536, PAGE_SIZE)
        }
        for address in resident:
            seen.add(address)
            raw = pages[address]
            pfn, actual_node = hexadecimal(raw["pfn"]), int(raw["node"])
            all_pfns.add(pfn)
            mapping_nodes[f"N{actual_node}" if 0 <= actual_node < 4 else "errors"] += 1
            totals[f"{domain}_resident_pages"] += 1
            evidence = details.get(address)
            if evidence is None:
                continue
            if (
                hexadecimal(evidence["pfn"]),
                int(evidence["node"]),
                hexadecimal(evidence["mapping_begin"]),
                evidence["path"],
                evidence["perms"],
                hexadecimal(evidence["file_offset"]),
            ) != (
                pfn,
                actual_node,
                begin,
                mapping["path"],
                mapping["perms"],
                offset + address - begin,
            ):
                findings.add(
                    phase,
                    "page_row_mismatch",
                    "Backing evidence differs from original resident query",
                    hex(address),
                )
            if (
                evidence["error"]
                or not evidence["kpageflags"]
                or not evidence["pagemap_entry"]
                or pfn == 0
            ):
                findings.add(
                    phase,
                    "unknown_page_evidence",
                    "Resident page has unavailable PFN/backing evidence",
                    hex(address),
                )
                totals[f"{domain}_unknown_pages"] += 1
                continue
            flags, entry = (
                hexadecimal(evidence["kpageflags"]),
                hexadecimal(evidence["pagemap_entry"]),
            )
            stable = bool(entry & PRESENT) and entry & ((1 << 55) - 1) == pfn
            if not stable or not boolean(evidence["pfn_recheck_same"]):
                findings.add(
                    phase,
                    "unstable_snapshot_page",
                    "Page changed during a single query",
                    hex(address),
                )
            actual_kind = page_kind(mapping["path"], mapping["perms"], flags, entry)
            backing[f"{domain}:{actual_kind}"] += 1
            if (
                executable
                and actual_kind != "file_cache"
                and not (address in closures and actual_kind == "anonymous")
            ):
                findings.add(
                    phase,
                    "code_not_file_cache",
                    (
                        "Executable inode alone does not prove "
                        "copied-file backing for a COW/anonymous page"
                    ),
                    f"{mapping['path']} {address:#x} backing={actual_kind}",
                )
            if evidence["page_kind"] != actual_kind or evidence["mapping_kind"] != kind:
                findings.add(
                    phase,
                    "backing_classification_mismatch",
                    "Saved classification contradicts raw page flags",
                    hex(address),
                )
            if actual_kind == "unknown_backing":
                findings.add(
                    phase,
                    "unknown_page_backing",
                    "Observed flags do not establish the page backing",
                    hex(address),
                )
                totals[f"{domain}_unknown_pages"] += 1
            if boolean(evidence["file_or_shared_anon"]) != bool(
                entry & FILE_OR_SHARED_ANON
            ) or boolean(evidence["exclusive"]) != bool(entry & EXCLUSIVE):
                findings.add(
                    phase,
                    "sharing_flags_mismatch",
                    "Sharing fields contradict raw pagemap bits",
                    hex(address),
                )
            count = int(evidence["mapcount"])
            sharing[
                "multiple_mappings"
                if count > 1
                else "one_mapping"
                if count == 1
                else "zero_or_unknown_mapping_count"
            ] += 1
            if count < 0:
                findings.add(
                    phase,
                    "invalid_mapcount",
                    "Negative physical mapcount",
                    hex(address),
                )
            zero = actual_kind == "kernel_zero_page"
            if zero:
                totals[f"{domain}_confirmed_kernel_zero_pages"] += 1
                if executable:
                    findings.add(
                        phase,
                        "zero_executable",
                        "Executable code resolves to kernel shared zero page",
                        hex(address),
                    )
            else:
                totals[f"{domain}_locality_required_pages"] += 1
                if actual_node == node:
                    totals[f"{domain}_local_pages"] += 1
                else:
                    findings.add(
                        phase,
                        "numa_query_error" if actual_node < 0 else "nonlocal_page",
                        "Ordinary resident page has unknown/nonlocal NUMA location",
                        f"{mapping['path']} {address:#x} node={actual_node}",
                    )
            if not executable:
                data_pfns[address] = pfn
                continue
            code_seen.add(address)
            recorded = code.get(address)
            if recorded is None:
                findings.add(
                    phase,
                    "missing_code_extent",
                    "Resident code has no saved folio extent",
                    hex(address),
                )
                continue
            if (
                recorded["path"],
                hexadecimal(recorded["pfn"]),
                int(recorded["node"]),
                hexadecimal(recorded["file_offset"]),
                hexadecimal(recorded["kpageflags"]) & (HEAD | TAIL),
            ) != (
                mapping["path"],
                pfn,
                actual_node,
                offset + address - begin,
                flags & (HEAD | TAIL),
            ):
                findings.add(
                    phase,
                    "code_row_mismatch",
                    "Code evidence differs from resident/backing evidence",
                    hex(address),
                )
            size = folio_pages(recorded)
            label = (
                "4k" if size == 1 else "exact_64k" if size == 16 else "other_compound"
            )
            totals[f"code_{label}_pages"] += 1
            totals[f"code_folio_{size * 4}k_pages"] += 1
            totals["code_runtime_closure_pages"] += int(address in closures)
            coverage[f"{label}_pages"] += 1
            coverage["resident_pages"] += 1
            fragment = address not in interior
            if fragment:
                fragment_kind = "small_mapping" if end - begin < 65536 else "boundary"
                coverage[f"{fragment_kind}_pages"] += 1
                totals[f"code_{fragment_kind}_pages"] += 1
            signatures[address] = (
                mapping["path"],
                offset + address - begin,
                pfn,
                actual_node,
                flags & (HEAD | TAIL),
                hexadecimal(recorded["folio_head_pfn"]),
                size,
            )
            if mode == "4k" and size != 1:
                findings.add(
                    phase,
                    "code_not_4k",
                    "4k condition contains compound code pages",
                    f"{mapping['path']} {address:#x} folio_pages={size}",
                )
            if mode == "64k" and size > 16:
                findings.add(
                    phase,
                    "code_not_exact_64k",
                    "64k condition contains a larger compound folio",
                    f"{mapping['path']} {address:#x} folio_pages={size}",
                )
        for key in ("N0", "N1", "N2", "N3", "errors"):
            if mapping_nodes[key] != int(mapping[key]):
                findings.add(
                    phase,
                    "node_totals_mismatch",
                    "Per-mapping node totals differ from raw page queries",
                    f"{begin:#x} {key}",
                )
        if not executable:
            continue
        for group in complete_starts:
            group_seen.add(group)
            coverage["complete_virtual_groups"] += 1
            present = [
                address
                for address in range(group, group + 65536, PAGE_SIZE)
                if address in code
            ]
            congruent = (offset + group - begin) % 65536 == 0
            fully_resident = len(present) == 16
            coverage["fully_resident_groups"] += int(fully_resident)
            coverage["nonresident_groups"] += int(not present)
            coverage["partly_resident_groups"] += int(
                bool(present) and not fully_resident
            )
            evidence = groups.get(group)
            if evidence is None:
                findings.add(
                    phase,
                    "missing_group_evidence",
                    "Aligned interior group has no saved group record",
                    hex(group),
                )
                continue
            contiguous = fully_resident and all(
                hexadecimal(code[address]["pfn"])
                == hexadecimal(code[group]["pfn"]) + index
                for index, address in enumerate(present)
            )
            aligned = fully_resident and hexadecimal(code[group]["pfn"]) % 16 == 0
            compound = bool(
                contiguous
                and aligned
                and hexadecimal(code[group]["kpageflags"]) & (HEAD | TAIL)
                and all(
                    hexadecimal(code[address]["kpageflags"]) & TAIL
                    for address in present[1:]
                )
            )
            exact = bool(
                compound
                and hexadecimal(code[group]["kpageflags"]) & HEAD
                and not hexadecimal(evidence["next_pfn_kpageflags"]) & TAIL
                and all(int(code[address]["folio_pages"]) == 16 for address in present)
            )
            local = sum(int(code[address]["node"]) == node for address in present)
            saved = (
                evidence["path"],
                hexadecimal(evidence["file_offset"]),
                int(evidence["present"]),
                int(evidence["expected_node_pages"]),
                boolean(evidence["contiguous"]),
                boolean(evidence["aligned"]),
                boolean(evidence["compound_at_least_64k"]),
                boolean(evidence["exact_64k"]),
            )
            if saved != (
                mapping["path"],
                offset + group - begin,
                len(present),
                local,
                contiguous,
                aligned,
                compound,
                exact,
            ):
                findings.add(
                    phase,
                    "group_evidence_mismatch",
                    "Group assertions disagree with raw per-page evidence",
                    hex(group),
                )
            coverage["exact_64k_groups"] += int(exact and congruent)
            totals["code_complete_virtual_groups"] += 1
            totals["code_fully_resident_groups"] += int(fully_resident)
            totals["code_exact_64k_groups"] += int(exact and congruent)
            all_exact = bool(present) and all(
                int(code[address]["folio_pages"]) == 16 for address in present
            )
            heads = {
                hexadecimal(code[address]["folio_head_pfn"]) for address in present
            }
            same_folio = (
                all_exact
                and len(heads) == 1
                and all(
                    hexadecimal(code[address]["pfn"])
                    == hexadecimal(code[address]["folio_head_pfn"])
                    + (address - group) // PAGE_SIZE
                    for address in present
                )
            )
            partial_exact = bool(not fully_resident and congruent and same_folio)
            coverage["partial_exact_64k_groups"] += int(partial_exact)
            totals["code_partial_exact_64k_groups"] += int(partial_exact)
            if mode == "64k" and all_exact and not (congruent and same_folio):
                findings.add(
                    phase,
                    "incomplete_or_non64k_interior",
                    "Resident exact64k pages disagree on aligned group backing",
                    hex(group),
                )
    if seen != set(pages) or code_seen != set(code) or group_seen != set(groups):
        findings.add(
            phase,
            "unmapped_evidence_rows",
            "Resident/code/group rows are not exactly covered by their mappings",
        )
    if not totals["code_resident_pages"] or not totals["data_locality_required_pages"]:
        findings.add(
            phase,
            "empty_validation_domain",
            "Both ordinary resident code and ordinary data are required",
        )
    if (
        mode == "64k"
        and totals["code_exact_64k_pages"] * 100 < totals["code_resident_pages"] * 99
    ):
        findings.add(
            phase,
            "code_not_exact_64k",
            "Exact64KB backing covers less than 99% of resident code pages",
        )
    if mode == "64k" and not totals["code_exact_64k_groups"]:
        findings.add(
            phase,
            "no_exact_64k_interior",
            "No complete exact64k interior group was observed",
        )
    return {
        "pid": pid,
        "start_ticks": start,
        "mount_namespace": summary.get("mount_namespace"),
        "original_mount_namespace": original,
        "evidence_sha256": evidence_hashes(folder),
        "counts": dict(totals),
        "backing_counts": dict(backing),
        "sharing_counts": dict(sharing),
        "excluded_scopes": excluded,
        "unique_resident_pfns": len(all_pfns),
        "coverage": {
            "code_residency": fraction(
                totals["code_resident_pages"], totals["code_mapped_pages"]
            ),
            "data_residency": fraction(
                totals["data_resident_pages"], totals["data_mapped_pages"]
            ),
            "code_locality": fraction(
                totals["code_local_pages"], totals["code_locality_required_pages"]
            ),
            "data_locality": fraction(
                totals["data_local_pages"], totals["data_locality_required_pages"]
            ),
            "exact64k_resident_code": fraction(
                totals["code_exact_64k_pages"], totals["code_resident_pages"]
            ),
            "exact64k_complete_resident_groups": fraction(
                totals["code_exact_64k_groups"], totals["code_fully_resident_groups"]
            ),
            "structural_fragments": fraction(
                totals["code_small_mapping_pages"] + totals["code_boundary_pages"],
                totals["code_resident_pages"],
            ),
        },
        "files": {path: dict(counts) for path, counts in file_coverage.items()},
        "runtime_closure_exceptions": list(closures.values()),
        "code_page_guard": guard_evidence,
        "code_signatures": signatures,
        "code_mappings": code_maps,
        "data_pfns": data_pfns,
    }


def verify(
    before: Path,
    after: Path,
    identity: Path,
    mode: str,
    node: int,
    coverage_policy: str = "strict",
    residency_policy: str = "strict",
) -> dict:
    if coverage_policy not in ("strict", "observe"):
        raise ValueError(f"Unknown code-page coverage policy: {coverage_policy}")
    if residency_policy not in ("strict", "observe"):
        raise ValueError(f"Unknown code-page residency policy: {residency_policy}")
    before, after, identity = before.resolve(), after.resolve(), identity.resolve()
    findings = Findings()
    phases = {}
    identity_bytes = identity.read_bytes()
    manifest = json.loads(identity_bytes)
    records = manifest["files"]
    files = {record["path"]: record for record in records}
    if len(files) != len(records):
        findings.add(
            "condition", "duplicate_identity_path", "Condition has duplicate file paths"
        )
    identity_hash = hashlib.sha256(identity_bytes).hexdigest()
    guard = guarded_code_metadata(manifest, files, findings)
    for label, folder in (("before", before), ("after", after)):
        try:
            phases[label] = verify_phase(
                folder, files, identity_hash, mode, node, label, findings, guard
            )
        except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
            findings.add(
                label,
                "invalid_snapshot",
                "Missing or malformed evidence prevents acceptance",
                str(exc),
            )
    stability = {}
    if len(phases) == 2:
        first, last = phases["before"], phases["after"]
        if (
            first["pid"],
            first["start_ticks"],
            first["mount_namespace"],
            first["original_mount_namespace"],
        ) != (
            last["pid"],
            last["start_ticks"],
            last["mount_namespace"],
            last["original_mount_namespace"],
        ):
            findings.add(
                "pair",
                "process_changed",
                "Before/after do not describe the same process and mount namespace",
            )
        if first["code_mappings"] != last["code_mappings"]:
            findings.add(
                "pair",
                "code_mappings_changed",
                "Executable mapping identity/layout changed during the round",
            )
        initial, final = first["code_signatures"], last["code_signatures"]
        closure_changed = (
            first["runtime_closure_exceptions"] != last["runtime_closure_exceptions"]
        )
        if closure_changed:
            findings.add(
                "pair",
                "runtime_closure_changed",
                (
                    "Runtime closure content/PFN/node/target identity "
                    "differs between endpoints"
                ),
            )
        added, removed = set(final) - set(initial), set(initial) - set(final)
        changed = [
            address
            for address in set(initial) & set(final)
            if initial[address] != final[address]
        ]
        stability = {
            "common_code_pages": len(set(initial) & set(final)),
            "newly_resident_code_pages": len(added),
            "no_longer_resident_code_pages": len(removed),
            "changed_code_pages": len(changed),
            "runtime_closure_changed": closure_changed,
            "common_data_pfns_changed": sum(
                first["data_pfns"][address] != last["data_pfns"][address]
                for address in set(first["data_pfns"]) & set(last["data_pfns"])
            ),
        }
        for addresses, code, message in (
            (
                added,
                "new_code_residency",
                (
                    "Valid first-use code pages need a repeated warmed "
                    "round under strict stability"
                ),
            ),
            (
                removed,
                "lost_code_residency",
                "Resident code pages disappeared during the round",
            ),
            (
                changed,
                "code_page_changed",
                "Code PFN/node/compound backing changed during the round",
            ),
        ):
            if addresses:
                findings.add(
                    "pair",
                    code,
                    message,
                    ", ".join(hex(value) for value in sorted(addresses)[:8]),
                    len(addresses),
                )
    for phase in phases.values():
        for key in ("code_signatures", "code_mappings", "data_pfns"):
            del phase[key]
    reasons = findings.rows()
    observed_codes = set()
    if coverage_policy == "observe":
        observed_codes.add("code_not_exact_64k")
    if residency_policy == "observe":
        observed_codes.update(
            ("new_code_residency", "lost_code_residency", "code_page_changed")
        )
    observations = [r for r in reasons if r["code"] in observed_codes]
    reasons = [r for r in reasons if r["code"] not in observed_codes]
    return {
        "schema_version": 1,
        "status": "fail" if reasons else "pass",
        "mode": mode,
        "coverage_policy": coverage_policy,
        "residency_policy": residency_policy,
        "minimum_exact64k_coverage": (
            0.99 if mode == "64k" and coverage_policy == "strict" else None
        ),
        "observations": observations,
        "expected_node": node,
        "identity": str(identity),
        "identity_sha256": identity_hash,
        "before": str(before),
        "after": str(after),
        "reasons": reasons,
        "phases": phases,
        "stability": stability,
        "needs_warmed_repeat": any(
            reason["code"] == "new_code_residency" for reason in reasons
        ),
        "scope": (
            "ordinary resident CPU mapping pages; counts are virtual-page "
            "observations unless marked unique"
        ),
        "exceptions": [
            (
                "confirmed KPF_ZERO_PAGE is excluded from "
                "locality-required data and never counted local"
            ),
            (
                "known device/kernel mappings have unknown "
                "residency/location and are outside ordinary-RAM "
                "acceptance"
            ),
            (
                "exact64KB resident-code coverage is observational; "
                "smaller folios retain actual sizes and remain in the denominator"
                if coverage_policy == "observe"
                else "64k requires at least 99% exact64KB resident-code backing; "
                "smaller folios retain actual sizes and remain in the denominator"
            ),
            (
                "at most one strictly identified libffi/ctypes runtime "
                "closure page remains counted as 4KB code; exact content "
                "and identity must match at both endpoints"
            ),
        ],
        "not_proven": [
            "CONT PTE bits",
            "unresident page backing/location",
            "device-mapping host-RAM locality",
            "continuous in-window stability between endpoint snapshots",
            "current bytes of source/copy files",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    parser.add_argument("--mode", choices=("4k", "64k"), required=True)
    parser.add_argument(
        "--coverage-policy", choices=("strict", "observe"), default="strict"
    )
    parser.add_argument(
        "--residency-policy", choices=("strict", "observe"), default="strict"
    )
    parser.add_argument("--node", type=int, choices=range(4), required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = verify(
            args.before,
            args.after,
            args.identity,
            args.mode,
            args.node,
            args.coverage_policy,
            args.residency_policy,
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = {
            "schema_version": 1,
            "status": "fail",
            "reasons": [{"code": "invalid_inputs", "message": str(exc)}],
        }
    payload = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    else:
        sys.stdout.write(payload)
    raise SystemExit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
