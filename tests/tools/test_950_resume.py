# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Interruption/restart contracts without GPU access or real CPUFreq writes."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from scripts.experiments_950 import Controls, conditions, initialize_results
from scripts.resume import archive, checkpoint, ready
from scripts.spe import capture, compact
from scripts.spe.resolve import file_identity
from scripts.spe.supervise import resume_state

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def controls(tmp_path):
    cpus = tmp_path / "cpu"
    policy = cpus / "cpufreq/policy14"
    policy.mkdir(parents=True)
    original = {
        "scaling_governor": "performance",
        "scaling_min_freq": "1200000",
        "scaling_max_freq": "2300000",
        "scaling_setspeed": "2300000",
        "cpuinfo_min_freq": "1200000",
        "cpuinfo_max_freq": "2300000",
        "scaling_available_governors": "performance userspace",
    }
    for name, value in original.items():
        (policy / name).write_text(value + "\n")
    for cpu in (14, 15):
        (cpus / f"cpu{cpu}").mkdir()
        (cpus / f"cpu{cpu}/cpufreq").symlink_to(policy)
    thp = tmp_path / "thp_exec_enabled"
    thp.write_text("0x0\n")
    return Controls(tmp_path / "restore.json", cpus, thp, "boot1"), policy


def test_matrix_has_two_distinct_default_baselines_and_twelve_fixed_conditions():
    four, large = conditions("4k"), conditions("64k")
    assert (len(four), len(large)) == (8, 6)
    assert len({row["id"] for row in four + large}) == 14
    assert [row["model"] for row in four if row["mhz"] is None] == [
        "Qwen3_8B",
        "GLM_4_7_Flash_4bit",
    ]
    assert all(row["model"] == "Qwen3_8B" for row in large)


def test_shared_policy_changed_once_and_recovered_after_process_disappears(controls):
    control, policy = controls
    control.frequency("14,15", 2100)
    control.exec_pages("0x2")
    saved = json.loads(control.journal.read_text())
    assert list(saved["policies"]) == [str(policy)]
    assert (policy / "scaling_min_freq").read_text().strip() == "2100000"
    # Simulate a process killed before its finally; next launch restores its journal.
    Controls(control.journal, control.cpu_root, control.thp, "boot1")
    assert not control.journal.exists()
    assert control.thp.read_text().strip() == "0x0"
    assert (policy / "scaling_governor").read_text().strip() == "performance"
    assert (policy / "scaling_min_freq").read_text().strip() == "1200000"
    assert (policy / "scaling_max_freq").read_text().strip() == "2300000"


def test_new_boot_uses_new_kernel_defaults_instead_of_old_policy_values(controls):
    control, policy = controls
    control.frequency("14", 2100)
    (policy / "scaling_governor").write_text("powersave\n")
    Controls(control.journal, control.cpu_root, control.thp, "boot2")
    assert (policy / "scaling_governor").read_text().strip() == "powersave"
    assert not control.journal.exists()


def test_unexposed_2500_is_not_forced_or_substituted(controls):
    control, policy = controls
    assert not control.supported("14", 2500)
    with pytest.raises(ValueError, match="does not expose"):
        control.frequency("14", 2500)
    assert not control.journal.exists()
    assert (policy / "scaling_governor").read_text().strip() == "performance"


def test_restore_runs_after_a_partial_frequency_write(controls, monkeypatch):
    control, policy = controls
    write = Path.write_text

    def fail_setspeed(path, value, *args, **kwargs):
        if path.name == "scaling_setspeed" and value == "2100000\n":
            raise OSError("driver rejected request")
        return write(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_setspeed)
    with pytest.raises(OSError, match="driver rejected"):
        try:
            control.frequency("14", 2100)
        finally:
            control.restore()
    assert (policy / "scaling_governor").read_text().strip() == "performance"
    assert (policy / "scaling_min_freq").read_text().strip() == "1200000"


def test_completed_round_reused_but_changed_data_is_not_silently_accepted(tmp_path):
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
    with pytest.raises(ValueError, match="changed"):
        ready(folder)


def test_partial_round_is_kept_in_internal_history(tmp_path):
    folder = tmp_path / "frequency"
    folder.mkdir()
    (folder / "benchmark.log").write_text("incomplete")
    assert not ready(folder)
    archive(folder)
    assert not folder.exists()
    assert (tmp_path / ".history/frequency/1/benchmark.log").read_text() == "incomplete"


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


def test_partial_run_rejects_configuration_change_before_resuming(tmp_path):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts/config.env").write_text(
        'MODEL=default\nsource "$TOPDOWN_CONFIG"\n'
    )
    config = tmp_path / "config.env"
    config.write_text("MODEL=qwen\nWORKER_CPUS=14,16\n")
    root = tmp_path / "run"
    monitor = tmp_path / ".monitor/run"
    monitor.mkdir(parents=True)
    frozen = {
        "files": {"kperf_instrument.py": {"sha256": "abc"}},
        "container": {"id": "same", "image": "same"},
    }
    args = Namespace(project=project, config=config)
    resume_state(args, root, monitor, frozen)
    assert (tmp_path / ".run_resume_contract.json").exists()
    config.write_text("MODEL=qwen\nWORKER_CPUS=168,170\n")
    with pytest.raises(ValueError, match="Measurement inputs changed"):
        resume_state(args, root, monitor, frozen)


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


def test_setup_creates_only_fourteen_stable_condition_directories(tmp_path):
    root = tmp_path / "Workload_Data"
    initialize_results(root)
    (root / conditions("4k")[0]["id"] / "partial.txt").write_text("saved")
    initialize_results(root)
    assert {p.name for p in root.iterdir()} == {
        row["id"] for row in conditions("4k") + conditions("64k")
    }
    assert (root / conditions("4k")[0]["id"] / "partial.txt").read_text() == "saved"
