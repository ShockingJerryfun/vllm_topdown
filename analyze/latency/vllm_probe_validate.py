"""Check that a benchmark round completed every requested prompt."""

import argparse
import json
import re
import sys
from pathlib import Path

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COMPLETED = re.compile(r"^\s*Successful requests:\s*(\d+)\s*$", re.MULTILINE)
FAILED = re.compile(r"^\s*Failed requests:\s*(\d+)\s*$", re.MULTILINE)


def validate(log: Path, expected: int) -> dict[str, int]:
    if expected <= 0:
        raise ValueError("Expected prompt count must be positive")
    text = ANSI_ESCAPE.sub("", log.read_text(encoding="utf-8", errors="replace"))
    completed = COMPLETED.findall(text)
    if "Serving Benchmark Result" not in text or len(completed) != 1:
        raise ValueError(f"{log}: benchmark result or successful request count is missing")
    if int(completed[0]) != expected:
        raise ValueError(f"{log}: completed {completed[0]} of {expected} requests")
    failed = FAILED.findall(text)
    if len(failed) != 1 or int(failed[0]) != 0:
        raise ValueError(f"{log}: benchmark failed request count is missing or nonzero")
    client_path = log.with_suffix(".client.json")
    if client_path.is_file():
        result = json.loads(client_path.read_text(encoding="utf-8"))
        payload = result.get("result", {})
        if (result.get("status") != "ok" or payload.get("completed") != expected
                or payload.get("failed", 0) != 0):
            raise ValueError(f"{client_path}: benchmark client result is incomplete")
    return {"expected": expected, "completed": int(completed[0]), "failed": int(failed[0])}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--expected-prompts", type=int, required=True)
    args = parser.parse_args()
    sys.stdout.write(json.dumps(validate(args.log, args.expected_prompts)) + "\n")


if __name__ == "__main__":
    main()
