# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Check that the selected PMU profile matches an AMD Zen 4 host."""

import platform
from pathlib import Path


def validate(system: str, machine: str, cpuinfo: str) -> None:
    fields = {}
    for line in cpuinfo.split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    family = int(fields.get("cpu family", "-1"))
    model = int(fields.get("model", "-1"))
    if (
        system != "Linux"
        or machine != "x86_64"
        or fields.get("vendor_id") != "AuthenticAMD"
        or family != 25
        or model < 0x10
        or model >> 4 in (0x2, 0x4, 0x5)
    ):
        raise RuntimeError("The zen4 profile requires an AMD Zen 4 Linux host")


if __name__ == "__main__":
    validate(
        platform.system(),
        platform.machine(),
        Path("/proc/cpuinfo").read_text(encoding="utf-8"),
    )
