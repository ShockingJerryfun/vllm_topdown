# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Exercise topology constraints and PID/thread/page acceptance without Linux/GPU."""

from pathlib import Path

import pytest

import kperf_instrument
from scripts import placement


@pytest.fixture
def linux(tmp_path, monkeypatch):
    def mapped(value):
        path = Path(value)
        if str(path).startswith(("/proc/", "/sys/")) or str(path) == "/proc":
            return tmp_path / str(path).lstrip("/")
        return path

    def write(name, text):
        path = mapped(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    monkeypatch.setattr(placement, "Path", mapped)
    for cpu in range(16):
        base = f"/sys/devices/system/cpu/cpu{cpu}"
        write(base + "/topology/physical_package_id", "0")
        write(base + "/topology/cluster_id", str(cpu // 8))
        write(
            base + "/topology/thread_siblings_list",
            f"{cpu // 2 * 2}-{cpu // 2 * 2 + 1}",
        )
        mapped(base + "/node0").mkdir()
    write("/proc/self/status", "Mems_allowed_list:\t0\n")
    masks = {
        0: set(range(16)),
        100: {0, 2, 4, 6, 8},
        110: {0, 2, 4, 6, 8},
        120: {0, 2},
        121: {0, 2},
    }
    monkeypatch.setattr(
        placement.os, "sched_getaffinity", lambda tid: masks[tid], raising=False
    )
    monkeypatch.setattr(
        placement.os,
        "sched_setaffinity",
        lambda tid, cpus: masks.__setitem__(tid, cpus),
        raising=False,
    )
    for name, value in {
        "WORKER_CPUS": "0,2",
        "WORKER_POOL_CPUS": "0,2,4,6",
        "SERVICE_CPUS": "8",
        "CLIENT_CPUS": "10",
        "WORKER_NUMA_NODE": "0",
    }.items():
        monkeypatch.setenv(name, value)
    for pid, parent, command in [
        (100, 1, "vllm serve"),
        (110, 100, "VLLM::EngineCore"),
        (120, 110, "VLLM::Worker"),
    ]:
        fields = ["S", str(parent), "100", "100"] + ["0"] * 15 + ["700"]
        write(f"/proc/{pid}/stat", f"{pid} (test) " + " ".join(fields))
        write(f"/proc/{pid}/cmdline", command + "\0")
        mapped(f"/proc/{pid}/task/{pid}").mkdir(parents=True)
    mapped("/proc/120/task/121").mkdir()
    write(
        "/proc/120/numa_maps",
        "1000 bind:0 anon=2 dirty=2 N0=2\n2000 bind:0 file=/lib/x N1=1\n",
    )
    return write, masks, mapped


@pytest.mark.parametrize("cpus", ["0", "0,2", "0,2,4,6"])
def test_one_two_four_physical_cores(linux, monkeypatch, cpus):
    monkeypatch.setenv("WORKER_CPUS", cpus)
    assert placement.validate_config()["WORKER_CPUS"] == sorted(
        placement.cpu_list(cpus)
    )


@pytest.mark.parametrize(
    "variable,value,match",
    [
        ("WORKER_POOL_CPUS", "0,1,2,4", "SMT"),
        ("WORKER_POOL_CPUS", "0,2,4,8", "cluster"),
        ("SERVICE_CPUS", "7", "overlap"),
        ("CLIENT_CPUS", "9", "overlap"),
        ("WORKER_CPUS", "0,2,4", "1/2/4"),
        ("WORKER_CPUS", "0,0", "Duplicate"),
        ("WORKER_CPUS", "2-0", "Reversed"),
        ("WORKER_CPUS", "0;uname", "Invalid"),
        ("WORKER_NUMA_NODE", "1", "NUMA"),
    ],
)
def test_invalid_placement_rejected(linux, monkeypatch, variable, value, match):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=match):
        placement.validate_config()


def test_container_missing_reserved_core_rejected(linux):
    _, masks, _ = linux
    masks[0].remove(6)
    with pytest.raises(ValueError, match="allowance"):
        placement.validate_config()


def test_only_owned_tree_bound_and_later_threads_checked(linux, tmp_path):
    write, masks, mapped = linux
    # An identically named unrelated Worker must not become the target.
    write("/proc/200/stat", "200 (other) S 1 200 200 " + "0 " * 15 + "701")
    write("/proc/200/cmdline", "VLLM::Worker\0")
    identity = tmp_path / "identity.json"
    result = placement.audit(100, identity, True)
    assert result["identity"]["worker"] == 120
    assert masks[100] == masks[110] == {8}
    assert masks[120] == masks[121] == {0, 2}
    assert result["pages"]["all_resident_pages"] == {"N0": 2, "N1": 1}
    mapped("/proc/120/task/122").mkdir()
    masks[122] = {0, 2}
    assert "122" in placement.audit(100, identity, False)["threads"]
    masks[122] = {4}
    with pytest.raises(RuntimeError, match="affinity"):
        placement.audit(100, identity, False)


def test_restart_or_configuration_change_requires_new_service(
    linux, tmp_path, monkeypatch
):
    _, _, mapped = linux
    identity = tmp_path / "identity.json"
    placement.audit(100, identity, True)
    monkeypatch.setenv("WORKER_CPUS", "0")
    with pytest.raises(RuntimeError, match="restart required"):
        placement.audit(100, identity, False)
    monkeypatch.setenv("WORKER_CPUS", "0,2")
    stat = mapped("/proc/120/stat")
    stat.write_text(stat.read_text().replace("700", "999"))
    with pytest.raises(RuntimeError, match="PID/start-time"):
        placement.audit(100, identity, False)


@pytest.mark.parametrize(
    "content",
    [
        "1000 default anon=1 N0=1",
        "1000 bind:0 anon=1 N1=1",
        "1000 bind:0 file=/x N0=1",
    ],
)
def test_memory_binding_downgrade_or_wrong_pages_rejected(content):
    with pytest.raises(RuntimeError):
        placement.page_distribution(content, 0)


def test_mixed_file_vma_node_counts_are_not_anonymous_page_locations():
    rows = "1000 bind:0 anon=2 N0=2\n2000 bind:0 file=/lib/x anon=1 N0=1 N1=8"
    result = placement.page_distribution(rows, 0)
    assert result["anonymous_resident_pages"] == {"N0": 2}
    assert result["mixed_mapping_resident_pages"] == {"N0": 1, "N1": 8}
    assert result["all_resident_pages"] == {"N0": 3, "N1": 8}


def test_fully_cow_file_vma_remote_anonymous_pages_rejected():
    with pytest.raises(RuntimeError, match="outside"):
        placement.page_distribution("1000 bind:0 file=/lib/x anon=2 N1=2", 0)


def test_core_probe_rejects_wrong_begin_and_end_tid(monkeypatch):
    monkeypatch.setattr(kperf_instrument, "STRICT_THREAD", True)
    monkeypatch.setattr(kperf_instrument, "STRICT_OWNER_TID", 10)
    monkeypatch.setattr(kperf_instrument.threading, "get_native_id", lambda: 11)
    monkeypatch.setattr(kperf_instrument, "ENABLED", True)
    monkeypatch.setattr(kperf_instrument, "TARGET_STAGE", "")
    monkeypatch.setattr(kperf_instrument, "ACTIVE", True)
    with pytest.raises(RuntimeError, match="begin: probe TID"):
        kperf_instrument.kperf_begin("sample")
    with pytest.raises(RuntimeError, match="end: probe TID"):
        kperf_instrument.kperf_finish("sample")
