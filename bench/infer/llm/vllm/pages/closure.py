# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Strict structural classifier for the observed AArch64 libffi/ctypes closure page.

This is one measured layout, not a general anonymous-code or libffi allowlist.
Instruction/closure layout: libffi v3.4.4 src/aarch64/ffi.c:795-869.
Allocator layout: src/dlmalloc.c TOP_FOOT_SIZE, init_top and 56-byte chunks.
Target file offsets are restricted to the inspected libffi.so.8.1.2 / CPython
3.13 ELF build hashes. Addresses and copy inodes resolve from the current run;
anonymous-page hashes are measured, never fixed across processes.
"""

from __future__ import annotations

import hashlib
import struct

if __package__:
    from .evidence import PAGE_SIZE, device_parts
else:
    from evidence import PAGE_SIZE, device_parts

TRAMPOLINE = bytes.fromhex("90000058f1ffff1000021fd600000000")
TARGETS = {
    "ffi_closure_SYSV": ("/usr/lib64/libffi.so.8.1.2", 0x6800),
    "ctypes_callback": (
        (
            "/usr/local/lib/python3.13/lib-dynload/_"
            "ctypes.cpython-313-aarch64-linux-gnu.so"
        ),
        0x161F8,
    ),
}
# Verified against evidence/anonymous_probe/targets/binaries.json, actual ELF
# bytes and libffi_closure.asm; a different build requires renewed evidence.
SUPPORTED_BUILDS = {
    "ffi_closure_SYSV": (
        "3343107f68508d8668e559a0f0f8bec56d2a31d47ec60882a12700f96423139c"
    ),
    "ctypes_callback": (
        "ad763c8ac89a717d9a7b82702d20c2d6bb782238827ee1f976a50d3c3f92ddcc"
    ),
}


def containing_mapping(address: int, mappings: list[dict]) -> dict:
    matches = [
        row
        for row in mappings
        if int(row["begin"], 16) <= address < int(row["end"], 16)
    ]
    if len(matches) != 1:
        raise ValueError(f"Closure pointer has no unique actual mapping: {address:#x}")
    return matches[0]


def frozen_target(address: int, role: str, mappings: list[dict], files: dict) -> dict:
    mapping = containing_mapping(address, mappings)
    expected_path, expected_offset = TARGETS[role]
    offset = int(mapping["offset"], 16) + address - int(mapping["begin"], 16)
    if (
        mapping["scope"] != "ordinary"
        or mapping["perms"] != "r-xp"
        or mapping["path"] != expected_path
        or offset != expected_offset
    ):
        raise ValueError(f"Unproven {role} target mapping/file offset")
    identity = files.get(expected_path)
    if identity is None or identity.get("task_generated_diagnostic"):
        raise ValueError(f"Missing ordinary frozen ELF identity for {role}")
    actual = tuple(int(part, 16) for part in mapping["device"].split(":")) + (
        int(mapping["inode"]),
    )
    expected = device_parts(int(identity["copy_device"])) + (
        int(identity["copy_inode"]),
    )
    source = (identity.get("source_device"), identity.get("source_inode"))
    if (
        actual != expected
        or None in source
        or source == (identity["copy_device"], identity["copy_inode"])
    ):
        raise ValueError(
            f"Target {role} does not resolve to an independent frozen copy inode"
        )
    if identity.get("sha256") != SUPPORTED_BUILDS[role]:
        raise ValueError(f"Target {role} has an unsupported ELF build SHA256")
    return {
        "role": role,
        "address": hex(address),
        "path": expected_path,
        "file_offset": hex(offset),
        "sha256": identity["sha256"],
        "copy_device": identity["copy_device"],
        "copy_inode": identity["copy_inode"],
    }


def classify_closure_page(payload: bytes, mappings: list[dict], files: dict) -> dict:
    """Account for every byte; reject unknown code, padding, pointers or layout."""
    if len(payload) != PAGE_SIZE or payload[:8] != bytes(8):
        raise ValueError("Closure exception requires one exact 4KB allocator page")
    cursor = 0
    offsets = []
    targets: dict[str, dict] = {}
    while (
        cursor + 64 <= PAGE_SIZE - 72
        and struct.unpack_from("<Q", payload, cursor + 8)[0] == 0x3B
    ):
        start = cursor + 16
        if payload[start : start + 16] != TRAMPOLINE:
            raise ValueError(f"Unrecognized AArch64 closure instructions at {start:#x}")
        handler, cif, callback, user_data = struct.unpack_from(
            "<QQQQ", payload, start + 16
        )
        for role, address in (
            ("ffi_closure_SYSV", handler),
            ("ctypes_callback", callback),
        ):
            target = frozen_target(address, role, mappings, files)
            if role in targets and target != targets[role]:
                raise ValueError("Closure chunks disagree on their target identity")
            targets[role] = target
        if cif != user_data + 40 or user_data % 8:
            raise ValueError(
                "Closure CIF/user-data relation differs from observed ctypes layout"
            )
        for pointer in (cif, user_data):
            mapping = containing_mapping(pointer, mappings)
            if (
                mapping["scope"] != "ordinary"
                or mapping["perms"] != "rw-p"
                or int(mapping["inode"]) != 0
            ):
                raise ValueError(
                    "Closure metadata pointer does not resolve to ordinary private data"
                )
        offsets.append(start)
        cursor += 56
    if not offsets:
        raise ValueError("No supported closure chunk was observed")
    # Fresh dlmalloc page: top chunk followed by the 72-byte overhead marker.
    free_size = PAGE_SIZE - 72 - cursor
    if (
        free_size < 32
        or struct.unpack_from("<Q", payload, cursor + 8)[0] != free_size | 1
    ):
        raise ValueError("Unknown allocator tail after closure chunks")
    if (
        any(payload[cursor + 16 : PAGE_SIZE - 64])
        or struct.unpack_from("<Q", payload, PAGE_SIZE - 64)[0] != 72
        or any(payload[PAGE_SIZE - 56 :])
    ):
        raise ValueError(
            "Unexplained bytes outside closure chunks and allocator metadata"
        )
    return {
        "kind": "libffi_ctypes_aarch64_runtime_closure",
        "page_bytes": PAGE_SIZE,
        "content_sha256": hashlib.sha256(payload).hexdigest(),
        "closure_count": len(offsets),
        "trampoline_offsets": [hex(value) for value in offsets],
        "chunk_bytes": 56,
        "target_libraries": list(targets.values()),
        "basis": (
            "exact trampoline, ctypes pointer relation, complete "
            "allocator layout, current frozen target mappings"
        ),
        "boundary": (
            "structural runtime closure identity; does not prove callbacks executed"
        ),
    }
