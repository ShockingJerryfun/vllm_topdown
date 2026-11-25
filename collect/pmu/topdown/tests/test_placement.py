"""Check placement defaults remain consistent through the service audit."""

import json
from pathlib import Path

import pytest

from adapters.vllm.scripts import placement


def test_audit_defaults_worker_cpu_node_to_memory_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {
        "WORKER_CPUS": "92,94",
        "WORKER_POOL_CPUS": "88,90,92,94",
        "WORKER_NUMA_NODE": "1",
        "SERVICE_CPUS": "256,258",
        "CLIENT_CPUS": "264",
        "API_CPUS": "256",
        "ENGINE_CPUS": "258",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("WORKER_CPU_NODE", raising=False)
    monkeypatch.delenv("CODE_PAGE_NUMA_NODE", raising=False)
    monkeypatch.setattr(placement, "process_table", dict)
    monkeypatch.setattr(
        placement, "identify",
        lambda api, table: {"api": 1, "engine": 2, "worker": 3, "starts": {}},
    )
    monkeypatch.setattr(placement, "executable_anonymous_addresses", lambda pid: set())
    monkeypatch.setattr(placement, "page_distribution", lambda *args: {})
    monkeypatch.setattr(placement, "anonymous_role_pages", lambda *args: {})
    original_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if str(path).startswith("/proc/"):
            return ""
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    identity_path = tmp_path / "identity.json"
    placement.audit(api=1, identity_path=identity_path, bind=True)
    saved = json.loads(identity_path.read_text())
    assert saved["config"]["WORKER_CPU_NODE"] == saved["config"]["WORKER_NUMA_NODE"] == "1"


def test_zen4_uses_shared_l3_for_worker_pool_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for cpu, sibling in ((64, 192), (65, 193)):
        base = tmp_path / f"cpu{cpu}"
        (base / "topology").mkdir(parents=True)
        (base / "topology/physical_package_id").write_text("1\n")
        (base / "topology/thread_siblings_list").write_text(f"{cpu},{sibling}\n")
        (base / "node1").mkdir()
        cache = base / "cache/index3"
        cache.mkdir(parents=True)
        (cache / "level").write_text("3\n")
        (cache / "type").write_text("Unified\n")
        (cache / "shared_cpu_list").write_text("64-71,192-199\n")
    monkeypatch.setenv("TOPDOWN_PMU_PROFILE", "zen4")
    monkeypatch.setattr(placement, "Path", lambda name: tmp_path / Path(name).name)
    first = placement.topology(64)
    second = placement.topology(65)
    assert first == (1, 64, 1, {64, 192})
    assert second == (1, 64, 1, {65, 193})

    (tmp_path / "cpu65/cache/index3/shared_cpu_list").write_text("66-71,194-199\n")
    with pytest.raises(ValueError, match="invalid shared L3"):
        placement.topology(65)
