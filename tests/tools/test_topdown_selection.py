# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Run the actual chip scheduler with mocked measurement and platform commands."""

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("chip", ["920b", "950"])
@pytest.mark.parametrize("profile,rounds", [("full", 29), ("end_to_end", 14)])
def test_chip_runner_selects_exact_rounds(tmp_path, chip, profile, rounds):
    scripts = tmp_path / "scripts"
    (scripts / chip).mkdir(parents=True)
    (tmp_path / "vllm").mkdir()
    site = tmp_path / "site"
    site.mkdir()
    (tmp_path / "kperf_instrument.py").touch()
    for name in (f"{chip}/run.sh", "session.sh"):
        original = ROOT / "scripts" / name
        if original.exists():
            shutil.copy2(original, scripts / name)
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "mock_python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    cp = tools / "cp"
    cp.write_text(
        f"#!{sys.executable}\nimport pathlib,sys\npathlib.Path(sys.argv[-1]).mkdir()\n"
    )
    cp.chmod(0o755)
    for name in ("lscpu", "cat"):
        stub = tools / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    record = tmp_path / "rounds"
    config = (ROOT / "scripts/config.env").read_text()
    config += (
        f"\nPYTHON_BIN={shlex.quote(str(python))}\nVLLM_SITE={shlex.quote(str(site))}\n"
    )
    (scripts / "config.env").write_text(config)
    runner = scripts / "run_one.sh"
    runner.write_text("""#!/usr/bin/env bash
set -eu
if [[ "$1" == service ]]; then
    trap 'exit 0' TERM
    while :; do sleep 0.1; done
fi
printf '%s|%s|%s|%s\\n' "$1" "${KPERF_TARGET:-}" "${KPERF_QUALIFIER:-}" \\
    "$RUN_ROOT" >> "$ROUND_RECORD"
""")
    runner.chmod(0o755)
    results = tmp_path / "results"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "RUN_ROOT": str(results),
        "ROUND_RECORD": str(record),
        "COLLECTION_PROFILE": profile,
    }
    output = subprocess.run(
        ["bash", str(scripts / chip / "run.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert output.returncode == 0, output.stderr
    rows = [row.split("|") for row in record.read_text().splitlines()]
    assert len(rows) == rounds
    e2e = [row for row in rows if row[1] == "execute_model_to_sample_tokens"]
    assert len(e2e) == 14
    assert all(
        row[2] == "run_fullgraph" and row[3].endswith("/end_to_end") for row in e2e
    )
    assert e2e[0][0] == "time"
    assert len({row[0] for row in e2e}) == 14
    assert (results / "collection_profile").read_text().strip() == profile
    if profile == "end_to_end":
        assert rows == e2e
    else:
        assert rows[-1][0] == "hotspot"


@pytest.mark.parametrize("binding_rejected", [False, True])
def test_placement_options_propagate_and_probe_failure_aborts(
    tmp_path, binding_rejected
):
    helper = tmp_path / "placement.py"
    helper.write_text("")
    numactl = tmp_path / "numactl"
    numactl.write_text("#!/bin/sh\nexit " + ("1" if binding_rejected else "0") + "\n")
    numactl.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    script = f"""
set -Eeuo pipefail
export WORKER_CPUS=0,2 WORKER_POOL_CPUS=0,2,4,6 WORKER_NUMA_NODE=0
export SERVICE_CPUS=8 CLIENT_CPUS=10
export PLACEMENT_MODE=worker_set COLLECTION_PROFILE=end_to_end
set -a
source {shlex.quote(str(ROOT / "scripts/config.env"))}
set +a
PYTHON_BIN={shlex.quote(sys.executable)}
SCRIPT_DIR={shlex.quote(str(tmp_path))}
RUN_DIR={shlex.quote(str(out))}
source {shlex.quote(str(ROOT / "scripts/placement.sh"))}
printf '%s\\n' "$VLLM_WORKER_MULTIPROC_METHOD" "$KPERF_STRICT_NUMA" \\
    "$WORKER_CPUS" "$COLLECTION_PROFILE" \\
    "${{BINDING_ARGS[@]}}" "${{CLIENT_PREFIX[@]}}"
"""
    result = subprocess.run(
        ["bash", "-c", script],
        text=True,
        capture_output=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    if binding_rejected:
        assert result.returncode != 0
        assert not result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == [
            "spawn",
            "1",
            "0,2",
            "end_to_end",
            "--distributed-executor-backend",
            "mp",
            "--numa-bind",
            "--numa-bind-nodes",
            "0",
            "--numa-bind-cpus",
            "0,2",
            "taskset",
            "-c",
            "10",
        ]


@pytest.mark.parametrize("chip,spe", [("920b", "auto"), ("950", "1")])
def test_main_entry_routes_spe_to_locked_full_supervisor(tmp_path, chip, spe):
    """A full main-entry run must not silently drop SPE or its task config."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("config.env", "run_topdown.sh", "run_experiment.sh"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    tools = tmp_path / "bin"
    tools.mkdir()
    docker = tools / "docker"
    docker.write_text('#!/bin/sh\n[ "$2" != -f ] || echo true\nexit 0\n')
    docker.chmod(0o755)
    host = tools / "host"
    host.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    host.chmod(0o755)
    config = tmp_path / "task.env"
    config.write_text(
        f"CHIP={chip}\nPROJECT={shlex.quote(str(tmp_path))}\n"
        f"SPE_ENABLE={spe}\nHOST_PYTHON={shlex.quote(str(host))}\n"
        "PLACEMENT_MODE=worker_set\nCOLLECTION_PROFILE=full\n"
        "WORKER_CPUS=252,254\nWORKER_POOL_CPUS=248,250,252,254\n"
        "SERVICE_CPUS=256,258\nCLIENT_CPUS=264\nWORKER_NUMA_NODE=3\n"
        "EXPERIMENT_LOCK=/tmp/task.lock\nSUBREAPER_BIN=/tmp/tini\n"
        "SPE_BINARY_CACHE=/tmp/binaries\n"
    )
    result = subprocess.run(
        ["bash", str(scripts / "run_topdown.sh"), str(config), str(tmp_path / "run")],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert "--spe\n" in result.stdout
    assert "--profile\nfull\n" in result.stdout
    assert f"--config\n{config}\n" in result.stdout
    assert f"--run\n{tmp_path / 'run'}\n" in result.stdout
    assert "--lock\n/tmp/task.lock\n" in result.stdout
