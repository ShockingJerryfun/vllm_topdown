# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Create byte-identical code and optional data copies for one private mount view.

Run in the target container with --source-root / when possible. A /proc/PID/root
source is also supported for canonical, nonsymlink paths from that PID's maps.
This command does not evict caches, mount files, or change kernel settings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import struct
from pathlib import Path, PurePosixPath

LOGGER = logging.getLogger(__name__)


def validate_path(value: str) -> str:
    """Reject paths unsafe for a whitespace-delimited, exact-path mount manifest."""
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or any(c.isspace() for c in value):
        raise ValueError(
            f"Expected canonical absolute path without whitespace: {value!r}"
        )
    if str(path) != value or value.startswith("//") or "\x00" in value:
        raise ValueError(f"Noncanonical path: {value!r}")
    return value


def mapped_files(maps_paths: list[Path]) -> set[str]:
    """Select ordinary executable file mappings, including generated launchers."""
    selected: set[str] = set()
    for maps_path in maps_paths:
        for line in maps_path.read_text().splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) != 6 or "x" not in fields[1]:
                continue
            name = fields[5]
            if not name.startswith("/") or name.startswith("/dev/"):
                continue
            selected.add(validate_path(name))
    return selected


def elf_segments(path: Path) -> list[dict[str, int]]:
    """Read PT_LOAD|PF_X file ranges, retaining alignment and boundary evidence."""
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:6] != b"\x7fELF\x02\x01":
            raise ValueError(f"Expected little-endian ELF64: {path}")
        (phoff,) = struct.unpack_from("<Q", header, 32)
        phsize, phcount = struct.unpack_from("<HH", header, 54)
        if phsize != 56 or phcount == 65535:
            raise ValueError(f"Unsupported ELF program-header layout: {path}")
        stream.seek(phoff)
        headers = stream.read(phsize * phcount)
    if len(headers) != phsize * phcount:
        raise ValueError(f"Truncated program headers: {path}")
    file_size = path.stat().st_size
    result = []
    for index in range(phcount):
        kind, flags, offset, address, _, size, _, alignment = struct.unpack_from(
            "<IIQQQQQQ", headers, index * phsize
        )
        if kind != 1 or not flags & 1 or not size:
            continue
        if offset + size > file_size:
            raise ValueError(f"ELF executable segment exceeds file: {path}")
        result.append(
            {
                "offset": offset,
                "size": size,
                "vaddr": address,
                "elf_alignment": alignment,
                "congruent_64k": int((address - offset) % 65536 == 0),
            }
        )
    if not result:
        raise ValueError(f"ELF has no executable file segment: {path}")
    return result


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(
    source_root: Path,
    paths: set[str],
    destination: Path,
    code_guard: str | None = None,
    *,
    data_paths: set[str] | None = None,
) -> None:
    """Freeze source identity, copy bytes, and make manifests for isolated tools."""
    if code_guard is not None:
        code_guard = validate_path(code_guard)
        paths = paths | {code_guard}
    if not paths:
        raise ValueError("The executable file selection is empty")
    data_paths = set() if data_paths is None else data_paths
    if paths & data_paths:
        raise ValueError("Code and data-only file selections must not overlap")
    paths = paths | data_paths
    if destination.exists():
        raise FileExistsError(
            f"Use a new task-owned condition directory: {destination}"
        )
    destination.mkdir(parents=True)
    copies = destination / "files"
    copies.mkdir()
    records = []
    segment_lines = []
    for name in sorted(paths):
        validate_path(name)
        source = source_root / name.lstrip("/")
        before = source.stat()
        if not source.is_file():
            raise ValueError(f"Not a regular source file: {source}")
        if name in data_paths:
            with source.open("rb") as stream:
                if stream.read(4) == b"\x7fELF":
                    raise ValueError(
                        f"Data-only selection must not contain ELF: {source}"
                    )
            segments = []
        else:
            segments = elf_segments(source)
        target = copies / name.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Real byte copy, never hardlink, symlink, reflink, or copy_file_range.
        with source.open("rb") as incoming, target.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        target.chmod(before.st_mode & 0o777)
        source_hash = digest(source)
        target_hash = digest(target)
        after, copied = source.stat(), target.stat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError(f"Source changed during copy: {source}")
        if source_hash != target_hash or (after.st_dev, after.st_ino) == (
            copied.st_dev,
            copied.st_ino,
        ):
            raise ValueError(f"Copy is not byte-identical and independent: {name}")
        records.append(
            {
                "path": name,
                "sha256": source_hash,
                "size": before.st_size,
                "source_device": before.st_dev,
                "source_inode": before.st_ino,
                "copy_device": copied.st_dev,
                "copy_inode": copied.st_ino,
                "segments": segments,
                **({"data_only": True} if name in data_paths else {}),
            }
        )
        segment_lines.extend(f"{name}\t{s['offset']}\t{s['size']}" for s in segments)
    (destination / "paths.txt").write_text("\n".join(sorted(paths)) + "\n")
    (destination / "segments.tsv").write_text("\n".join(segment_lines) + "\n")
    manifest = {
        "source_root": str(source_root),
        "copy_root": str(copies.absolute()),
        "files": records,
        "file_count": len(records),
        "bytes": sum(record["size"] for record in records),
        "base_page_size_requested": 4096,
        "code_modes": {
            "4k": "read-only random base-page prefault",
            "64k": "executable 64K-stride prefault; kernel chooses folios",
        },
        "contiguous_pte_bits_observed": False,
    }
    if code_guard is not None:
        guard_record = next(
            record for record in records if record["path"] == code_guard
        )
        manifest["code_page_guard"] = {
            "path": code_guard,
            "sha256": guard_record["sha256"],
            "method": "executable_vma_madv_nohugepage",
        }
    (destination / "identity.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (copies / ".task_owned_copies").write_text(
        "Separate-inode task copies; no shared cache eviction.\n"
    )
    LOGGER.info(
        "Prepared %s files, %s executable segments", len(records), len(segment_lines)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path("/"))
    parser.add_argument("--maps", type=Path, action="append", default=[])
    parser.add_argument(
        "--paths", type=Path, help="Explicit executable-file paths, one per line"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--data-paths",
        type=Path,
        help="Optional non-ELF data paths, not code-prefaulted",
    )
    parser.add_argument(
        "--code-guard",
        help=(
            "Canonical ELF audit-library path, copied and prefaulted like ordinary code"
        ),
    )
    args = parser.parse_args()
    paths = mapped_files(args.maps)
    if args.paths:
        paths.update(
            validate_path(line) for line in args.paths.read_text().splitlines() if line
        )
    data_paths = (
        {
            validate_path(line)
            for line in args.data_paths.read_text().splitlines()
            if line
        }
        if args.data_paths
        else set()
    )
    prepare(
        args.source_root, paths, args.output, args.code_guard, data_paths=data_paths
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
