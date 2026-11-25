"""Validate and record the host identity for an adapter PMU profile."""

import argparse
import json
import platform
import sys
from pathlib import Path

from topdown.runner import validate_platform


def identity(profile: str, system: str, machine: str, cpuinfo: str) -> dict[str, str]:
    validate_platform(profile, system, machine, cpuinfo)
    fields = {}
    for line in cpuinfo.split("\n\n", 1)[0].splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return {
        "profile": profile, "system": system, "machine": machine,
        "vendor": fields.get("vendor_id", ""),
        "family": fields.get("cpu family", ""), "model": fields.get("model", ""),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=("zen4",))
    args = parser.parse_args()
    cpuinfo = Path("/proc/cpuinfo").read_text(encoding="utf-8")
    result = identity(args.profile, platform.system(), platform.machine(), cpuinfo)
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
