# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Interruption/restart contracts without GPU access or real CPUFreq writes."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.resume import checkpoint, prepare, ready
from scripts.spe import capture, compact
from scripts.spe.resolve import file_identity
from scripts.spe.supervise import resume_state

ROOT = Path(__file__).resolve().parents[2]


def test_completed_round_reuses_existing_raw_data_without_hash_contract(tmp_path):
    folder = tmp_path / "topdown"
    folder.mkdir()
    for name, content in {
        "benchmark.log": "Successful requests: 1\n",
        "run.env": "events=0x11\n",
        "measurement.log": "data",
        "stop.json": "{}",
    }.items():
        (folder / name).write_text(content)
    checkpoint(folder)
    assert ready(folder)
    (folder / "measurement.log").write_text("altered")
    assert ready(folder)
    (folder / "measurement.log").unlink()
    assert not ready(folder)


def test_partial_round_is_removed_before_recollection(tmp_path):
    folder = tmp_path / "frequency"
    folder.mkdir()
    (folder / "benchmark.log").write_text("incomplete")
    assert not ready(folder)
    assert not prepare(folder)
    assert not folder.exists()
    assert not (tmp_path / ".history").exists()


def test_real_chip_loop_resumes_missing_rounds_without_repeating_completed_ones(
    tmp_path,
):
    scripts = tmp_path / "scripts"
    (scripts / "950").mkdir(parents=True)
    (tmp_path / "vllm").mkdir()
    (tmp_path / "site").mkdir()
    (tmp_path / "kperf_instrument.py").touch()
    for name in ("950/run.sh", "session.sh", "resume.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        f"#!{sys.executable}\n"
        """
import os,sys,subprocess
from pathlib import Path
from argparse import Namespace
if len(sys.argv)>1 and sys.argv[1].endswith('/resume.py'):
    raise SystemExit(subprocess.call([sys.executable,*sys.argv[1:]]))
if len(sys.argv)>1 and sys.argv[1].endswith('/parse_run.py'):
    p=Path(sys.argv[2]);(p/'collection_quality.csv').write_text('status\\nok\\n')
"""
    )
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
    config = (ROOT / "scripts/config.env").read_text()
    config += (
        f"\nPYTHON_BIN={shlex.quote(str(python))}\n"
        f"VLLM_SITE={shlex.quote(str(tmp_path / 'site'))}\n"
        "RESUME_COLLECTION=1\nFREQUENCY_ENABLE=1\n"
    )
    (scripts / "config.env").write_text(config)
    stub = scripts / "run_one.sh"
    stub.write_text(
        f"#!{sys.executable}\n"
        """
import os,sys,time,signal,json,subprocess
from pathlib import Path
from argparse import Namespace
root=Path(os.environ['RUN_ROOT']);label=sys.argv[1]
if label=='service':
    signal.signal(signal.SIGTERM,lambda *args:sys.exit(0))
    while True: time.sleep(.01)
p=root/label;p.mkdir(parents=True)
with Path(os.environ['ROUND_RECORD']).open('a') as f:f.write(str(p)+'\\n')
fail=Path(os.environ['FAIL_ONCE'])
if label=='backend_memory' and not fail.exists():
    fail.touch();raise SystemExit(9)
for name,content in {'benchmark.log':'Successful requests: 1\\n','run.env':'model=test',
                     'measurement.log':'sample','stop.json':'{}',
                     'perf_report.txt':'hotspot','summary.json':'{}',
                     'frequency.csv':'frequency'}.items():
    (p/name).write_text(content)
subprocess.run([sys.executable,str(Path(__file__).with_name('resume.py')),'commit',str(p)],check=True)
"""
    )
    stub.chmod(0o755)
    record = tmp_path / "rounds.txt"
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "RUN_ROOT": str(tmp_path / "results"),
        "ROUND_RECORD": str(record),
        "FAIL_ONCE": str(tmp_path / "failed"),
        "COLLECTION_PROFILE": "full",
    }
    command = ["bash", str(scripts / "950/run.sh")]
    first = subprocess.run(command, env=env, capture_output=True, timeout=20)
    assert first.returncode == 9, first.stderr
    finished_before = record.read_text().splitlines()[:-1]
    second = subprocess.run(command, env=env, capture_output=True, timeout=30)
    assert second.returncode == 0, second.stderr.decode()
    all_rounds = record.read_text().splitlines()
    assert len(set(all_rounds)) == 30
    assert all(all_rounds.count(name) == 1 for name in finished_before)
    assert len(all_rounds) == 31
    third = subprocess.run(command, env=env, capture_output=True, timeout=30)
    assert third.returncode == 0, third.stderr.decode()
    assert record.read_text().splitlines() == all_rounds


def test_partial_run_clears_only_derived_outputs_before_resuming(tmp_path):
    root = tmp_path / "run"
    monitor = tmp_path / ".monitor/run"
    monitor.mkdir(parents=True)
    raw = root / "topdown/measurement.log"
    raw.parent.mkdir(parents=True)
    raw.write_text("raw")
    (root / "Topdown.xlsx").write_text("derived")
    (root / "complete.json").write_text("{}")
    (root / "acceptance.json").write_text("{}")
    (root / "evidence").mkdir()
    (root / ".history/old").mkdir(parents=True)
    (tmp_path / ".run_resume_contract.json").write_text("{}")
    (tmp_path / ".monitor/.history/run/1").mkdir(parents=True)
    resume_state(root, monitor)
    assert raw.read_text() == "raw"
    assert not monitor.exists()
    assert not list(root.glob("*.xlsx"))
    assert not (root / "complete.json").exists()
    assert not (root / "acceptance.json").exists()
    assert not (root / "evidence").exists()
    assert not (root / ".history").exists()
    assert not (tmp_path / ".run_resume_contract.json").exists()
    assert not (tmp_path / ".monitor/.history/run").exists()


@pytest.mark.parametrize(
    "compound,truncated,expected",
    [(False, False, "pass"), (True, False, "fail"), (False, True, "fail")],
)
def test_native_page_check_does_not_accept_compound_or_unreadable_pages(
    tmp_path, monkeypatch, compound, truncated, expected
):
    proc = tmp_path / "proc"
    (proc / "123").mkdir(parents=True)
    (proc / "123/maps").write_text(
        "00000000-00002000 r-xp 00000000 00:01 1 /lib/test.so\n"
    )
    entry = ((1 << 63) | 1).to_bytes(8, "little")
    (proc / "123/pagemap").write_bytes(entry if truncated else entry * 2)
    (proc / "kpageflags").write_bytes(
        bytes(8) + ((1 << 15) if compound else 0).to_bytes(8, "little")
    )
    original = Path.open

    def mapped_open(path, *args, **kwargs):
        if str(path).startswith("/proc/"):
            path = proc / path.relative_to("/proc")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mapped_open)
    monkeypatch.setattr(capture.os, "sysconf", lambda name: 4096)
    assert capture.native_code_pages(123)["status"] == expected


def test_spe_resume_finishes_only_its_verified_remaining_raw_deletion(tmp_path):
    root = tmp_path / "spe"
    analysis = root / "analysis"
    analysis.mkdir(parents=True)
    (root / "data").mkdir()
    (root / "spe_capture.json").write_text('{"raw_retention":"selected"}')
    for name in (
        "samples.jsonl.gz",
        "pc_summary.json",
        "manifest.json",
        "windows.json",
    ):
        (analysis / name).write_bytes(b"retained")
    (analysis / "audit.json").write_text('{"selected_samples":2,"pc_count":1}')
    first, remaining = root / "data/first", root / "data/remaining"
    first.write_bytes(b"raw1")
    remaining.write_bytes(b"raw2")
    receipt = {
        "status": "pass",
        "raw_retention": "selected",
        "deletion_complete": False,
        "selected_samples": 2,
        "pc_count": 1,
        "retained_files": [
            file_identity(p) for p in [root / "spe_capture.json", *analysis.iterdir()]
        ],
        "original_inputs": [file_identity(first), file_identity(remaining)],
    }
    (analysis / "retention.json").write_text(json.dumps(receipt))
    first.unlink()  # Power loss after deleting only the first raw file.
    assert compact.finish_pending_retention(root)["deletion_complete"]
    assert not remaining.exists()
    assert compact.verify_retention(root)["selected_samples"] == 2
