"""Exercise the real vLLM round scheduler without launching a model."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from topdown.runner import load_groups


def test_vllm_scheduler_runs_22_groups_for_parent_and_eight_stages(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    adapter = workspace / "adapters/vllm"
    adapter.parent.mkdir(parents=True)
    shutil.copytree(source / "adapters/vllm", adapter)
    (workspace / "topdown").symlink_to(source / "topdown", target_is_directory=True)
    scripts = adapter / "scripts"
    record = tmp_path / "rounds"
    runner = scripts / "run_one.sh"
    runner.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "$1" == service ]]; then
    trap 'exit 0' TERM
    while :; do sleep 0.1; done
fi
printf '%s|%s|%s\\n' "$1" "${KPERF_TARGET:-}" "${KPERF_QUALIFIER:-}" >> "$ROUND_RECORD"
"""
    )
    runner.chmod(0o755)
    for name in ("parse_run.py", "build_xlsx.py", "920b/summary.py"):
        (scripts / name).write_text("")
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in *'import os, sys, torch, vllm'*) exit 0;; esac\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n"
    )
    python.chmod(0o755)
    cp = tools / "cp"
    cp.write_text(
        f"#!{sys.executable}\nimport pathlib,sys\n"
        "(pathlib.Path(sys.argv[-1])/'utils').mkdir(parents=True)\n"
    )
    cp.chmod(0o755)
    source_vllm = tmp_path / "source"
    (source_vllm / "vllm").mkdir(parents=True)
    site = tmp_path / "site"
    site.mkdir()
    config = tmp_path / "config.env"
    config.write_text(
        f"PYTHON_BIN={shlex.quote(str(python))}\n"
        f"VLLM_SITE={shlex.quote(str(site))}\n"
        f"VLLM_SOURCE_ROOT={shlex.quote(str(source_vllm))}\n"
        "ANON_THP_MODE=on\nRANDOM_OUTPUT_LEN=3\n"
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "ROUND_RECORD": str(record),
        "COLLECTION_PROFILE": "full",
        "ZERO_PORT_HOTSPOT": "0",
    }
    env.pop("CODE_PAGE_CONDITION", None)
    output = subprocess.run(
        ["bash", str(adapter / "run.sh"), str(config), str(tmp_path / "results")],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )
    assert output.returncode == 0, output.stderr
    rows = [line.split("|") for line in record.read_text().splitlines()]
    pipeline = [row[0] for row in rows if not row[1] and row[0] in load_groups("920b")]
    parent = [row[0] for row in rows if row[1] == "execute_model_to_sample_tokens"
              and row[0] in load_groups("920b")]
    assert pipeline == parent == list(load_groups("920b"))
    assert all(row[2] == "run_fullgraph" for row in rows if row[1])
    assert [row[0] for row in rows].count("hotspot") == 1
    assert rows[0][0] == "benchmark_before" and rows[-1][0] == "benchmark_after"
