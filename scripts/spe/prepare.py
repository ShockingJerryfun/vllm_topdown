# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Add window markers only to a fresh SPE runtime overlay."""

import argparse
import difflib
import hashlib
import importlib.util
import json
import py_compile
import shutil
from pathlib import Path


def require_real_parents(root: Path, target: Path) -> None:
    """Reject linked directories inside the owned tree before replacing files."""
    directory = root
    if directory.is_symlink():
        raise ValueError(f"SPE preparation parent is a symlink: {directory}")
    for part in target.parent.relative_to(root).parts:
        directory /= part
        if directory.is_symlink():
            raise ValueError(f"SPE preparation parent is a symlink: {directory}")


def prepare(runtime: Path, run: Path) -> None:
    path = runtime / "vllm/v1/worker/gpu/cudagraph_utils.py"
    cache = Path(importlib.util.cache_from_source(str(path)))
    marker_target = runtime / "spe_marker.py"
    evidence = run / "evidence"
    evidence_paths = (evidence / "overlay.diff", evidence / "overlay.json")
    for target in (path, cache, marker_target):
        require_real_parents(runtime, target)
    for target in evidence_paths:
        require_real_parents(run, target)
    for target in (marker_target, *evidence_paths):
        if target.is_symlink():
            raise ValueError(f"SPE preparation output is a symlink: {target}")
    original = path.read_text()
    updated = original
    replacements = (
        (
            "from collections import defaultdict\n",
            (
                "from collections import defaultdict\n"
                "from spe_marker import clock, record_full, record_replay\n"
            ),
        ),
        (
            "        self.graphs[desc].replay()\n",
            (
                "        spe_start = clock()\n        self.graphs[desc].replay()\n"
                "        record_replay(spe_start, clock())\n"
            ),
        ),
        (
            '        kperf_begin("run_fullgraph")\n',
            '        spe_start = clock()\n        kperf_begin("run_fullgraph")\n',
        ),
        (
            '            kperf_finish("run_fullgraph")\n',
            (
                '            kperf_finish("run_fullgraph")\n'
                "            record_full(spe_start, clock())\n"
            ),
        ),
    )
    for before, after in replacements:
        if updated.count(before) != 1:
            raise ValueError(f"Non-unique SPE marker anchor: {before!r}")
        updated = updated.replace(before, after)
    path.unlink()
    path.write_text(updated)
    # cp -rs retains installed bytecode as symlinks. Replace only the overlay's
    # cache entry, never the installed cache that it points at.
    if cache.is_symlink():
        cache.unlink()
    py_compile.compile(str(path), doraise=True)
    marker = Path(__file__).with_name("marker.py")
    shutil.copyfile(marker, marker_target)
    evidence.mkdir(exist_ok=True)
    (evidence / "overlay.diff").write_text(
        "".join(
            difflib.unified_diff(
                original.splitlines(True),
                updated.splitlines(True),
                fromfile="maintained/cudagraph_utils.py",
                tofile="SPE/cudagraph_utils.py",
            )
        )
    )
    (evidence / "overlay.json").write_text(
        json.dumps(
            {
                "source_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "overlay_sha256": hashlib.sha256(updated.encode()).hexdigest(),
                "marker_sha256": hashlib.sha256(marker.read_bytes()).hexdigest(),
                "pmu": "disabled",
                "scope": "SPE diagnostic service only",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("run", type=Path)
    args = parser.parse_args()
    prepare(args.runtime, args.run)
