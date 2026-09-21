# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Verify retained SPE packets, then optionally remove this capture's raw perf file."""

import argparse
import gzip
import json
import logging
import time
from collections import Counter
from pathlib import Path

if __package__:
    from .decode import load_windows, select_window
    from .records import normalize_packets, read_packet
    from .resolve import file_identity
else:
    from decode import load_windows, select_window
    from records import normalize_packets, read_packet
    from resolve import file_identity


LOGGER = logging.getLogger(__name__)


def verify_selected(root: Path, analysis: Path) -> dict:
    """Replay retained raw packets without depending on the discarded perf file."""
    metadata = json.loads((root / "spe_capture.json").read_text())
    audit = json.loads((analysis / "audit.json").read_text())
    manifest = json.loads((analysis / "manifest.json").read_text())
    if audit["status"] != "pass" or manifest["status"] != "pass":
        raise ValueError("Only a successfully normalized capture can be compacted")
    windows = load_windows(root, metadata)
    starts = [window["start"] for window in windows]
    counts: Counter = Counter()
    requests: Counter = Counter()
    with gzip.open(analysis / "samples.jsonl.gz", "rt") as source:
        for line in source:
            row = json.loads(line)
            packets = []
            for entry in row["raw_packets"]:
                raw = bytes.fromhex(entry["hex"])
                packet = read_packet(
                    raw, 0, int(entry["offset"]), int(entry["file_offset"])
                )
                if len(packet.raw) != len(raw):
                    raise ValueError("Retained packet has trailing bytes")
                packets.append(packet)
            replay = normalize_packets(packets)
            if any(row[key] != value for key, value in replay.items()):
                raise ValueError("Retained raw packets disagree with normalized fields")
            window, reason = select_window(
                replay, windows, starts, metadata["host_tid"]
            )
            if reason != "selected" or any(
                row[key] != window[key] for key in ("request", "step")
            ):
                raise ValueError("Retained sample does not match a formal window")
            counts[row["pc_key"]] += 1
            requests[str(row["request"])] += 1
    summaries = json.loads((analysis / "pc_summary.json").read_text())
    if dict(counts) != {row["pc_key"]: row["samples"] for row in summaries}:
        raise ValueError("Selected packet counts disagree with PC summary")
    if (
        sum(counts.values()) != audit["selected_samples"]
        or dict(requests) != audit["request_counts"]
    ):
        raise ValueError("Selected packet counts disagree with capture audit")
    return {
        "status": "pass",
        "selected_samples": sum(counts.values()),
        "pc_count": len(counts),
        "requests": dict(requests),
    }


def compact(root: Path, discard_raw: bool = False) -> dict:
    root = root.resolve()
    analysis = root / "analysis"
    result = verify_selected(root, analysis)
    metadata_path = root / "spe_capture.json"
    metadata = json.loads(metadata_path.read_text())
    manifest = json.loads((analysis / "manifest.json").read_text())
    declared = {Path(row["path"]).resolve(): row for row in manifest["inputs"]}
    raw_files = []
    for entry in metadata["perf_files"]:
        for key in ("path", "packets_text", "memory_text"):
            if not entry.get(key):
                continue
            path = root / entry[key]
            if path.is_symlink() or path.resolve().parent != root / "data":
                raise ValueError("Raw deletion is restricted to this run's data folder")
            identity = file_identity(path)
            if identity != declared.get(path.resolve()):
                raise ValueError("Raw input changed since accepted normalization")
            raw_files.append(identity)
    if discard_raw and metadata.get("raw_retention") != "selected":
        raise ValueError("Capture did not opt into selected-only raw retention")
    retained = [
        file_identity(path)
        for path in sorted(analysis.iterdir())
        if path.is_file() and path.name != "retention.json"
    ]
    retained.append(file_identity(metadata_path))
    result.update(
        raw_retention="selected" if discard_raw else "all",
        original_inputs=raw_files,
        retained_files=retained,
        raw_bytes=sum(item["size"] for item in raw_files),
        selected_bytes=(analysis / "samples.jsonl.gz").stat().st_size,
        verified_ns=time.time_ns(),
        deletion_complete=False,
    )
    receipt = analysis / "retention.json"
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    if discard_raw:
        for entry in raw_files:
            Path(entry["path"]).unlink()
        result["deletion_complete"] = True
        receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result


def verify_retention(root: Path) -> dict:
    """Verify the replay receipt's file hashes without repeating the packet scan."""
    root = root.resolve()
    receipt = json.loads((root / "analysis/retention.json").read_text())
    if not (
        receipt.get("status") == "pass"
        and receipt.get("raw_retention") == "selected"
        and receipt.get("deletion_complete") is True
        and receipt.get("selected_samples", 0) > 0
    ):
        raise ValueError("SPE selected replay receipt is incomplete")
    files = receipt["retained_files"]
    metadata_files = [
        row for row in files if Path(row["path"]).name == "spe_capture.json"
    ]
    if len(metadata_files) != 1:
        raise ValueError("SPE replay receipt lacks one capture identity")
    original_root = Path(metadata_files[0]["path"]).parent
    seen = set()
    for row in files:
        relative = Path(row["path"]).relative_to(original_root)
        if relative in seen or ".." in relative.parts:
            raise ValueError("SPE replay receipt has duplicate or escaping paths")
        seen.add(relative)
        actual = file_identity(root / relative)
        if any(actual[key] != row[key] for key in ("sha256", "size")):
            raise ValueError(f"SPE retained file changed: {relative}")
    required = {Path("spe_capture.json")} | {
        Path("analysis") / name
        for name in (
            "samples.jsonl.gz",
            "pc_summary.json",
            "audit.json",
            "manifest.json",
            "windows.json",
        )
    }
    if not required <= seen:
        raise ValueError("SPE replay receipt is missing required files")
    metadata = json.loads((root / "spe_capture.json").read_text())
    audit = json.loads((root / "analysis/audit.json").read_text())
    if metadata.get("raw_retention") != "selected" or any(
        receipt[key] != audit[key] for key in ("selected_samples", "pc_count")
    ):
        raise ValueError("SPE replay receipt differs from capture/decoder")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--discard-raw", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    result = compact(args.run, args.discard_raw)
    LOGGER.info(
        "Verified %d retained samples; raw retention: %s",
        result["selected_samples"],
        result["raw_retention"],
    )


if __name__ == "__main__":
    main()
