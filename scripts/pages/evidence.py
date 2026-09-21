# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Pure page-evidence classification; mapping pathname never establishes COW."""

HEAD = 1 << 15
TAIL = 1 << 16
ANON = 1 << 12
KSM = 1 << 21
ZERO = 1 << 24
FILE_OR_SHARED_ANON = 1 << 61
EXCLUSIVE = 1 << 56
PRESENT = 1 << 63
PAGE_SIZE = 4096


def device_parts(device: int) -> tuple[int, int]:
    """Decode Linux dev_t on any review host, including macOS."""
    major = ((device >> 8) & 0xFFF) | ((device >> 32) & ~0xFFF)
    minor = (device & 0xFF) | ((device >> 12) & ~0xFF)
    return major, minor


def mapping_kind(path: str, permissions: str) -> str:
    if path.startswith("/dev/shm/") or path in ("/dev/zero", "/dev/zero (deleted)"):
        return "shared_ram_mapping"
    if path.startswith("/dev/"):
        return "device_mapping"
    if path in ("[vdso]", "[vsyscall]") or path.startswith("[vvar"):
        return "kernel_special_mapping"
    if path == "[heap]":
        return "heap_mapping"
    if path.startswith("/"):
        return "file_mapping"
    return "anonymous_mapping"


def page_kind(path: str, permissions: str, flags: int, entry: int) -> str:
    """Use observed flags for backing type; raw sharing bits remain separate."""
    if flags & ZERO:
        return "kernel_zero_page"
    if flags & KSM:
        return "ksm_anonymous"
    mapped = mapping_kind(path, permissions)
    if flags & ANON:
        if mapped == "file_mapping" and permissions.endswith("p"):
            return "private_cow_in_file_mapping"
        if entry & FILE_OR_SHARED_ANON:
            return "shared_anonymous"
        return "anonymous"
    if entry & FILE_OR_SHARED_ANON:
        if mapped == "shared_ram_mapping":
            return "shared_ram_file_backing"
        return "file_cache"
    return "unknown_backing"


def complete_file_groups(begin: int, end: int, offset: int) -> range:
    """Group by backing file offset; ASLR need not align the virtual address."""
    return range(begin + (-offset) % 65536, end - 65535, 65536)
