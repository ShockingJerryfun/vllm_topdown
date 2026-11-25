"""Probe behavior with the kernel-facing calls replaced by deterministic fakes."""

import hashlib
import json
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from topdown import probe


def test_runtime_identity_records_virtual_and_real_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "real.py"
    source.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "_classes.py").write_text("unrelated\n", encoding="utf-8")
    missing = tmp_path / "missing.py"
    modules = {
        "torch": SimpleNamespace(__file__=str(source)),
        "torch.classes": SimpleNamespace(__file__="_classes.py"),
        "torch.ops": SimpleNamespace(__file__="_ops.py"),
        "torch.missing": SimpleNamespace(__file__=str(missing)),
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KPERF_RUNTIME_IDENTITY_DIR", str(tmp_path / "identity"))
    monkeypatch.setenv("KPERF_IDENTITY_PREFIXES", "torch")
    monkeypatch.setattr(probe, "RUNTIME_RECORDED", False)
    monkeypatch.setattr(probe, "ENABLED", False)
    monkeypatch.setattr(probe, "sys", SimpleNamespace(
        modules=modules, executable="python", version="test", path=[]
    ))
    monkeypatch.setattr(probe.os, "sched_getaffinity", lambda pid: {0}, raising=False)
    read_text = Path.read_text

    def read_proc(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path("/proc/self/stat"):
            return "test stat"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_proc)
    probe.kperf_begin("work")
    identity = json.loads(next((tmp_path / "identity").glob("*.json")).read_text())
    assert identity["modules"]["torch"] == {
        "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()
    }
    for name, filename in (("torch.classes", "_classes.py"), ("torch.ops", "_ops.py"),
                           ("torch.missing", str(missing))):
        assert identity["modules"][name] == {
            "path": filename, "sha256": None, "status": "unresolved"
        }
    assert probe.RUNTIME_RECORDED


@pytest.fixture
def controlled(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []
    for name in (
        "MODE", "EVENTS", "NAMES", "SCOPE", "PMU_NAME", "ENABLED", "OWNER_TID",
        "TARGET_STAGE", "QUALIFIER_STAGE", "CALL", "NAME", "QUALIFIED", "ACTIVE",
        "WALL_START_NS", "THREAD_START_NS", "WALL_OVERHEAD_NS", "THREAD_OVERHEAD_NS",
    ):
        monkeypatch.setattr(probe, name, getattr(probe, name))
    monkeypatch.setattr(probe, "CONTROL_ENABLED", True)
    monkeypatch.setattr(probe, "STRICT_THREAD", False)
    monkeypatch.setattr(probe, "OWNER_TID", 0)
    monkeypatch.setattr(probe, "ACTIVE", False)
    monkeypatch.setattr(probe, "COUNTER_GROUPS", [])
    monkeypatch.setattr(probe, "GROUP_TIMES", {})
    monkeypatch.setattr(probe, "emit", messages.append)
    return messages


def test_region_finishes_on_exception_and_rejects_overlap(controlled: list[str]) -> None:
    probe.configure("time")
    with pytest.raises(ValueError, match="workload error"), probe.measure("outer"):
        with pytest.raises(RuntimeError, match="Nested"), probe.measure("inner"):
            pass
        raise ValueError("workload error")
    assert any(message.startswith("KPERF_TIME,outer,") for message in controlled)
    with probe.measure("next"):
        sum(range(100))
    assert any(message.startswith("KPERF_TIME,next,") for message in controlled)
    probe.configure("disabled")


def test_runtime_switch_keeps_group_time_deltas_and_closes_events(
    controlled: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    operations = []
    monkeypatch.setattr(probe, "open_event", lambda *args: 10)
    monkeypatch.setattr(probe, "event_id", lambda fd: 110)
    monkeypatch.setattr(probe.os, "close", lambda fd: operations.append(("close", fd)))
    monkeypatch.setattr(probe.fcntl, "ioctl", lambda *args: None)
    times = iter((100, 240))

    def fake_read(fd: int, size: int) -> bytes:
        total = next(times)
        return struct.pack("<QQQQQ", 1, total, total, 17, 110)

    monkeypatch.setattr(probe.os, "read", fake_read)
    probe.configure("pmu", "0x11", "cycles")
    for _ in range(2):
        with probe.measure("work"):
            pass
    records = [message for message in controlled if message.startswith("KPERF,")]
    assert records == ["KPERF,work,1,100,100,1,17", "KPERF,work,2,140,140,1,17"]
    probe.configure("disabled")
    assert operations == [("close", 10)]


def test_non_owner_thread_cannot_collect(controlled: list[str]) -> None:
    probe.configure("time")
    errors = []

    def other_thread() -> None:
        try:
            with probe.measure("other"):
                pass
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1 and "owner" in errors[0]
    probe.configure("disabled")


def test_missing_event_id_is_not_reported_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = struct.pack("<QQQQQ", 1, 10, 10, 5, 77)
    monkeypatch.setattr(probe.os, "read", lambda fd, size: payload)
    with pytest.raises(OSError, match="event ID"):
        probe.read_group([3], [88])
