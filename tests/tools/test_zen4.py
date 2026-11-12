# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Validate the Zen 4 scheduler and its platform-specific report."""

import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from openpyxl import load_workbook

from scripts.zen4 import groups, identity, metrics

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scheduler(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scripts = workspace / "scripts"
    shutil.copytree(ROOT / "scripts", scripts)
    (workspace / "vllm").mkdir()
    (workspace / "kperf_instrument.py").touch()
    site = tmp_path / "site"
    site.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *'zen4/identity.py'*) exit 0;;\n"
        "  *'import os, sys, torch, vllm'*) exit 0;;\n"
        "esac\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    python.chmod(0o755)
    cp = tools / "cp"
    cp.write_text(
        f"#!{sys.executable}\nimport pathlib,sys\n"
        "(pathlib.Path(sys.argv[-1])/'utils').mkdir(parents=True)\n"
    )
    cp.chmod(0o755)
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        "if [[ $1 == inspect ]]; then\n"
        "  [[ ${2:-} != -f ]] || echo true\n  exit 0\nfi\n"
        "[[ $1 == exec ]] || exit 1\nshift\n"
        "while [[ ${1:-} == -e ]]; do\n"
        '  shift\n  [[ $1 != *=* ]] || export "$1"\n  shift\ndone\n'
        'shift\nexec "$@"\n'
    )
    docker.chmod(0o755)
    runner = scripts / "run_one.sh"
    runner.write_text(
        "#!/usr/bin/env bash\nset -eu\n"
        'if [[ "$1" == service ]]; then\n'
        "  trap 'exit 0' TERM\n  while :; do sleep 0.1; done\nfi\n"
        'exec "$MOCK_PYTHON" "$MOCK_ROUND" "$@"\n'
    )
    runner.chmod(0o755)
    config = tmp_path / "baseline.env"
    config.write_text(
        "CHIP=zen4\n"
        f"PROJECT={shlex.quote(str(workspace))}\n"
        f"MODEL={shlex.quote(str(model))}\nCONTAINER=zen4-test\n"
        f"PYTHON_BIN={shlex.quote(str(python))}\n"
        f"VLLM_SITE={shlex.quote(str(site))}\n"
        "SPE_ENABLE=0\nANON_THP_MODE=on\nRANDOM_OUTPUT_LEN=3\n"
        "COLLECTION_PROFILE=full\n"
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "TOPDOWN_CONFIG": str(config),
        "ROUND_RECORD": str(tmp_path / "rounds.jsonl"),
        "MOCK_PYTHON": sys.executable,
        "MOCK_ROUND": str(ROOT / "tests/tools/fixtures/zen4_round.py"),
    }
    return scripts, tmp_path / "results", env


def test_zen4_scheduler_generates_parent_phases_and_workbook(scheduler) -> None:
    scripts, root, env = scheduler
    result = subprocess.run(
        ["bash", str(scripts / "run_topdown.sh"), env["TOPDOWN_CONFIG"], str(root)],
        env={**env, "RUN_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads((root / "report_config.json").read_text())
    events = {group["name"]: group["events"] for group in config["groups"]}
    assert len(events) == 14
    records = [
        json.loads(line) for line in Path(env["ROUND_RECORD"]).read_text().splitlines()
    ]
    for parent in (False, True):
        selected = [
            item
            for item in records
            if item["label"] in events and bool(item["target"]) == parent
        ]
        assert [(item["label"], item["codes"]) for item in selected] == list(
            events.items()
        )
    assert records[0]["label"] == "benchmark_before"
    assert records[-1]["label"] == "benchmark_after"
    assert sum(item["label"] == "hotspot" for item in records) == 1
    with (root / "collection_quality.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        quality = list(csv.DictReader(stream))
    assert len(quality) == 135
    assert all(row["status"] == "ok" and row["valid"] == "2" for row in quality)
    book = load_workbook(next(root.glob("*.xlsx")))
    assert book.sheetnames == ["Topdown", "Hotspot"]
    sheet = book["Topdown"]
    assert sheet.freeze_panes == "B2"
    assert sheet["B1"].value == "execute_model_to_sample_tokens"
    rows = {str(row[0].value).lstrip("- "): row for row in sheet.iter_rows(min_row=2)}
    assert rows["FrontendBound"][1].value == pytest.approx(0.15)
    assert rows["BackendBound"][1].value == pytest.approx(0.20)
    assert rows["SMTContention"][1].value == pytest.approx(0.01)
    assert rows["l1i missrate"][1].value == pytest.approx(0.15)
    assert rows["L1 Bound"][1].value == "未采集"
    assert rows["L3 missrate（共享范围）"][1].value == "未采集"
    assert "Benchmark" in rows
    book.close()


def test_zen4_scheduler_rejects_multiplexed_samples(scheduler) -> None:
    scripts, root, env = scheduler
    result = subprocess.run(
        ["bash", str(scripts / "zen4/run.sh")],
        env={**env, "RUN_ROOT": str(root), "FAKE_MULTIPLEX": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid_rows" in result.stderr
    assert not list(root.glob("*.xlsx"))


def make_l3_topology(tmp_path: Path) -> tuple[Path, Path]:
    cpu_root = tmp_path / "cpu"
    pmu = tmp_path / "amd_l3"
    pmu.mkdir()
    (pmu / "cpumask").write_text("0,8,64,72\n")
    (pmu / "format").mkdir()
    for name, value in {
        "event": "config:0-7",
        "umask": "config:8-15",
        "coreid": "config:42-44",
        "enallslices": "config:46",
        "threadmask": "config:56-57",
    }.items():
        (pmu / "format" / name).write_text(value)
    for cpu, core in ((68, 4), (69, 5)):
        base = cpu_root / f"cpu{cpu}"
        topology = base / "topology"
        topology.mkdir(parents=True)
        (topology / "thread_siblings_list").write_text(f"{cpu},{cpu + 128}\n")
        (topology / "core_id").write_text(f"{core}\n")
        cache = base / "cache/index3"
        cache.mkdir(parents=True)
        (cache / "level").write_text("3\n")
        (cache / "type").write_text("Unified\n")
        (cache / "shared_cpu_list").write_text("64-71,192-199\n")
    return cpu_root, pmu


def test_zen4_filtered_l3_uses_worker_topology(tmp_path: Path) -> None:
    cpu_root, pmu = make_l3_topology(tmp_path)
    group, representative = groups.l3_group(cpu_root, pmu, [68, 69])
    assert representative == 64
    assert group["events"] == [
        "l3_access_0",
        "l3_miss_0",
        "l3_access_1",
        "l3_miss_1",
        "0xc0",
    ]
    assert group["raw_events"] == [
        "0x10050000000ff04",
        "0x100500000000104",
        "0x10054000000ff04",
        "0x100540000000104",
        "0xc0",
    ]
    with pytest.raises(ValueError, match="one or two"):
        groups.l3_group(cpu_root, pmu, [68, 69, 70])


def test_zen4_scheduler_includes_optional_l3(scheduler, tmp_path: Path) -> None:
    scripts, root, env = scheduler
    cpu_root, pmu = make_l3_topology(tmp_path)
    cpuinfo = tmp_path / "cpuinfo"
    cpuinfo.write_text("vendor_id : AuthenticAMD\ncpu family : 25\nmodel : 17\n")
    helper = scripts / "zen4/groups.py"
    content = helper.read_text()
    content = content.replace('"/sys/devices/system/cpu"', repr(str(cpu_root)))
    content = content.replace('"/sys/bus/event_source/devices/amd_l3"', repr(str(pmu)))
    content = content.replace('"/proc/cpuinfo"', repr(str(cpuinfo)))
    helper.write_text(content)
    result = subprocess.run(
        ["bash", str(scripts / "zen4/run.sh")],
        env={
            **env,
            "RUN_ROOT": str(root),
            "ZEN4_L3_ENABLE": "1",
            "WORKER_CPUS": "68,69",
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    records = [
        json.loads(line) for line in Path(env["ROUND_RECORD"]).read_text().splitlines()
    ]
    l3 = [row for row in records if row["label"] == "common_l3"]
    assert len(l3) == 2 and all(row["scope"] == "uncore" for row in l3)
    with (root / "collection_quality.csv").open(
        encoding="utf-8-sig", newline=""
    ) as stream:
        assert len(list(csv.DictReader(stream))) == 144
    book = load_workbook(next(root.glob("*.xlsx")))
    rows = {
        str(row[0].value).lstrip("- "): row
        for row in book["Topdown"].iter_rows(min_row=2)
    }
    assert rows["L3 missrate（共享范围）"][1].value == pytest.approx(
        70 / 220, abs=0.0001
    )
    assert rows["L3 mpki（共享范围）"][1].value == pytest.approx(28)
    assert "共享域" in rows["L3 missrate（共享范围）"][0].comment.text
    book.close()


def test_zen4_profile_rejects_other_cpu_families() -> None:
    info = "vendor_id : AuthenticAMD\ncpu family : 25\nmodel : 17\n"
    identity.validate("Linux", "x86_64", info)
    with pytest.raises(RuntimeError, match="Zen 4"):
        identity.validate(
            "Linux", "x86_64", info.replace("AuthenticAMD", "GenuineIntel")
        )


def test_l3_metrics_use_only_shared_l3_and_same_round_instructions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "common_l3/parsed/run_fullgraph.csv"
    path.parent.mkdir(parents=True)
    path.write_text(
        "valid,l3_access_0,l3_miss_0,l3_access_1,l3_miss_1,0xc0\n1,100,10,200,20,1000\n"
    )
    result = metrics.stage_metrics(tmp_path, "run_fullgraph")
    assert result["L3 missrate（共享范围）"] == pytest.approx(0.1)
    assert result["L3 mpki（共享范围）"] == pytest.approx(30)
