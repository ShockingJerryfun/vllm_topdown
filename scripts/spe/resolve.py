# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Resolve PCs from the capture's maps and exact ELF snapshots."""

import hashlib
import mmap
import re
import shutil
import struct
import subprocess
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Any

IDENTITY_FIELDS = (
    "pc_key",
    "binary",
    "binary_sha256",
    "binary_build_id",
    "file_offset",
    "elf_pc",
    "function",
    "function_start",
    "function_end",
    "instruction",
    "instruction_bytes",
    "resolution_status",
    "function_status",
    "instruction_status",
)


def file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
    }


def elf_metadata(path: Path) -> dict[str, Any]:
    """Read ELF64 load segments and nonzero-sized STT_FUNC symbol intervals."""
    with (
        path.open("rb") as source,
        mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data,
    ):
        if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"Expected little-endian ELF64 snapshot: {path.name}")
        phoff, shoff = struct.unpack_from("<QQ", data, 32)
        phsize, phnum, shsize, shnum = struct.unpack_from("<HHHH", data, 54)
        if phsize < 56 or phoff + phsize * phnum > len(data):
            raise ValueError("Invalid ELF program header table")
        if shnum and (shsize < 64 or shoff + shsize * shnum > len(data)):
            raise ValueError("Invalid ELF section header table")
        loads = []
        for number in range(phnum):
            values = struct.unpack_from("<IIQQQQQQ", data, phoff + number * phsize)
            kind, flags, offset, virtual, _, size, _, _ = values
            if kind == 1:
                loads.append((offset, virtual, size, flags))
        sections = [
            struct.unpack_from("<IIQQQQIIQQ", data, shoff + n * shsize)
            for n in range(shnum)
        ]
        functions = set()
        build_id = None
        for section in sections:
            _, kind, _, _, offset, size, link, _, _, entry_size = section
            if kind not in (2, 7, 11):
                continue
            if offset + size > len(data):
                raise ValueError("ELF section extends beyond snapshot")
            if kind == 7:
                cursor = offset
                while cursor + 12 <= offset + size:
                    name_size, desc_size, note_type = struct.unpack_from(
                        "<III", data, cursor
                    )
                    name_start = cursor + 12
                    desc_start = name_start + ((name_size + 3) & ~3)
                    following = desc_start + ((desc_size + 3) & ~3)
                    if following > offset + size:
                        raise ValueError("Truncated ELF note")
                    if (
                        note_type == 3
                        and data[name_start : name_start + name_size] == b"GNU\x00"
                    ):
                        build_id = data[desc_start : desc_start + desc_size].hex()
                    cursor = following
                continue
            if not entry_size or entry_size < 24 or link >= len(sections):
                raise ValueError("Invalid ELF symbol table")
            strings = sections[link]
            string_offset, string_size = strings[4], strings[5]
            if string_offset + string_size > len(data):
                raise ValueError("Invalid ELF string table")
            for cursor in range(offset, offset + size, entry_size):
                name_index, info, _, defined, value, length = struct.unpack_from(
                    "<IBBHQQ", data, cursor
                )
                if info & 15 != 2 or not defined or not length:
                    continue
                if name_index >= string_size:
                    raise ValueError("Invalid ELF symbol name offset")
                start = string_offset + name_index
                stop = data.find(b"\x00", start, string_offset + string_size)
                if stop < 0:
                    raise ValueError("Unterminated ELF symbol name")
                name = data[start:stop].decode("utf-8", errors="replace")
                functions.add((value, value + length, name))
        return {"loads": loads, "functions": sorted(functions), "build_id": build_id}


def resolve_pcs(
    pcs: set[str], root: Path, metadata: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], list[str]]:
    mappings = []
    for line in (root / metadata["maps_file"]).read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5 or "x" not in fields[1]:
            continue
        start, end = (int(part, 16) for part in fields[0].split("-"))
        mappings.append(
            (
                start,
                end,
                int(fields[2], 16),
                fields[5] if len(fields) == 6 else "[anonymous]",
            )
        )
    mappings.sort()
    starts = [mapping[0] for mapping in mappings]
    snapshots = {item["mapped_path"]: item for item in metadata.get("binaries", [])}
    if len(snapshots) != len(metadata.get("binaries", [])):
        raise ValueError("Duplicate binary mapped_path entries")
    resolved = {}
    by_binary: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for text in sorted(pcs, key=lambda item: int(item, 16)):
        pc = int(text, 16)
        row = dict.fromkeys(IDENTITY_FIELDS)
        row.update(
            pc=text,
            pc_key=f"unresolved:{text}",
            resolution_status="mapping_missing",
            function_status="unknown",
            instruction_status="unknown",
        )
        index = bisect_right(starts, pc) - 1
        if index >= 0 and pc < mappings[index][1]:
            start, _, offset, mapped_path = mappings[index]
            row.update(
                binary=mapped_path,
                file_offset=hex(pc - start + offset),
                pc_key=f"unresolved:{mapped_path}:{start:x}:{pc - start + offset:x}",
                resolution_status="binary_snapshot_missing",
            )
            by_binary[mapped_path].append(row)
        resolved[text] = row
    inputs = []
    warnings = []
    objdump = metadata.get("tools", {}).get("objdump") or shutil.which("objdump")
    for mapped_path, rows in by_binary.items():
        specification = snapshots.get(mapped_path)
        if specification is None:
            continue
        path = root / specification["path"]
        identity = file_identity(path)
        if (
            specification.get("sha256")
            and identity["sha256"] != specification["sha256"]
        ):
            raise ValueError(f"Binary SHA256 mismatch: {path.name}")
        info = elf_metadata(path)
        if (
            specification.get("build_id")
            and info["build_id"] != specification["build_id"]
        ):
            raise ValueError(f"Binary build ID mismatch: {path.name}")
        inputs.append(identity)
        functions = info["functions"]
        function_starts = [item[0] for item in functions]
        with path.open("rb") as binary:
            for row in rows:
                offset = int(row["file_offset"], 16)
                row.update(
                    binary_sha256=identity["sha256"],
                    binary_build_id=info["build_id"],
                    pc_key=f"sha256:{identity['sha256']}:{offset:x}",
                )
                candidates = {
                    virtual + offset - begin
                    for begin, virtual, size, flags in info["loads"]
                    if begin <= offset < begin + size and flags & 1
                }
                if len(candidates) != 1:
                    row["resolution_status"] = "elf_executable_segment_ambiguous"
                    continue
                address = candidates.pop()
                binary.seek(offset)
                raw = binary.read(4)
                if len(raw) != 4:
                    raise ValueError("Sampled instruction extends beyond ELF snapshot")
                row.update(
                    elf_pc=hex(address),
                    instruction_bytes=raw.hex(),
                    resolution_status="exact_binary_offset",
                )
                index = bisect_right(function_starts, address) - 1
                # A sized symbol is evidence of boundaries. Never guess a function
                # end from the next symbol in a stripped binary.
                if index >= 0 and address < functions[index][1]:
                    start, end, name = functions[index]
                    row.update(
                        function=name,
                        function_start=hex(start),
                        function_end=hex(end),
                        function_status="sized_elf_symbol",
                    )
        if objdump:
            warnings.extend(disassemble(path, rows, objdump))
        else:
            warnings.append(
                f"No objdump executable; instructions unresolved for {path.name}"
            )
    return resolved, inputs, warnings


def disassemble(path: Path, rows: list[dict[str, Any]], executable: str) -> list[str]:
    addresses = {
        int(row["elf_pc"], 16): row for row in rows if row["elf_pc"] is not None
    }
    groups: list[list[int]] = []
    for address in sorted(addresses):
        # Amortize ELF loading without dumping an unbounded sparse address range.
        if (
            groups
            and address - groups[-1][-1] <= 65536
            and address - groups[-1][0] < 1048576
        ):
            groups[-1].append(address)
        else:
            groups.append([address])
    for group in groups:
        process = subprocess.run(
            [
                executable,
                "-d",
                f"--start-address={group[0]}",
                f"--stop-address={group[-1] + 4}",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if process.returncode:
            return [
                f"objdump could not decode {path.name}: {process.stderr.strip()[:240]}"
            ]
        for line in process.stdout.splitlines():
            match = re.match(
                r"^\s*([0-9a-fA-F]+):\s+([0-9a-fA-F]{8}|(?:[0-9a-fA-F]{2}\s+){3}[0-9a-fA-F]{2})\s+(.+)$",
                line,
            )
            if match is None or int(match[1], 16) not in addresses:
                continue
            row = addresses[int(match[1], 16)]
            encoded = match[2]
            actual = (
                bytes.fromhex(encoded)
                if " " in encoded
                else int(encoded, 16).to_bytes(4, "little")
            )
            if actual.hex() != row["instruction_bytes"]:
                raise ValueError(f"Disassembly byte mismatch in {path.name}")
            row.update(
                instruction=match[3].strip(), instruction_status="bytes_verified"
            )
    return []
