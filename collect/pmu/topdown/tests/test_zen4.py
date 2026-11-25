"""Zen 4 formulas, raw event encoding, and standalone collection."""

import csv
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import load_workbook

from topdown import probe
from topdown.profiles import zen4
from topdown.runner import collect, collect_raw, load_groups, validate_platform


CPUINFO = "vendor_id : AuthenticAMD\ncpu family : 25\nmodel : 17\n"


def write_rows(root: Path, group: str, values: dict[str, int | float]) -> None:
    path = root / group / "parsed" / "work.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"valid": 1, **values}
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=row)
        writer.writeheader()
        writer.writerow(row)


def test_group_layout_and_amd_extended_configs() -> None:
    groups = load_groups("zen4")
    assert len(groups) == 8
    assert max(map(len, groups.values())) == 5
    assert groups["slots"] == [
        "0x76", "0x1000001a0", "0x100001ea0", "0x1000060a0", "0x7aa"
    ]
    assert groups["frontend"] == ["0x76", "0x1000001a0", "0x1060001a0"]
    assert groups["l2_ratio"] == ["0xf960", "0xff70", "0xff71", "0xff72", "0x964"]
    assert {"0xc0", "0x964", "0xff71", "0xff72"} == set(groups["l2_mpki"])
    assert {"0xc0", "0xc2", "0xc3"} <= set(groups["branch"])


def test_perf_event_open_receives_full_amd_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def syscall(number: int, attr_ptr: object, pid: int, cpu: int, group: int, flags: int) -> int:
        attr = ctypes.cast(attr_ptr, ctypes.POINTER(probe.PerfEventAttr)).contents
        captured.update(config=attr.config, flags=attr.flags, pid=pid, cpu=cpu, group=group)
        return 7

    monkeypatch.setattr(probe, "LIBC", SimpleNamespace(syscall=syscall))
    monkeypatch.setattr(probe, "PERF_EVENT_OPEN", 298)
    monkeypatch.setattr(probe.platform, "system", lambda: "Linux")
    assert probe.open_event(0x1060001A0, 4, 0, -1, -1, True, True) == 7
    assert captured == {
        "config": 0x1060001A0,
        "flags": probe.PERF_EXCLUDE_HV | probe.PERF_DISABLED | probe.PERF_PINNED,
        "pid": 0, "cpu": -1, "group": -1,
    }


def test_formulas_use_synchronized_group_denominators(tmp_path: Path) -> None:
    write_rows(tmp_path, "time", {"wall_time_us": 100, "thread_cpu_time_us": 80})
    write_rows(tmp_path, "slots", {
        "0x76": 100, "0x1000001a0": 120, "0x100001ea0": 240,
        "0x1000060a0": 60, "0x7aa": 180,
    })
    write_rows(tmp_path, "retiring", {
        "0x76": 200, "0x7aa": 900, "0xc1": 600, "0x1000000c2": 120, "0xc0": 400,
        "time_enabled": 100, "time_running": 100,
    })
    write_rows(tmp_path, "frontend", {"0x76": 400, "0x1000001a0": 600, "0x1060001a0": 40})
    write_rows(tmp_path, "backend", {
        "0x76": 250, "0x100001ea0": 750, "0xa2d6": 60, "0x2d6": 100,
    })
    write_rows(tmp_path, "speculation", {
        "0x76": 300, "0x7aa": 1500, "0xc1": 900, "0xc3": 30, "0x96": 10,
    })
    write_rows(tmp_path, "l2_ratio", {
        "0xf960": 1000, "0xff70": 100, "0xff71": 50, "0xff72": 20, "0x964": 130,
    })
    write_rows(tmp_path, "l2_mpki", {"0xc0": 2000, "0x964": 80, "0xff71": 10, "0xff72": 10})
    write_rows(tmp_path, "branch", {"0x76": 900, "0xc0": 1000, "0xc2": 400, "0xc3": 40})

    result = zen4.stage_metrics(tmp_path, "work")
    assert result["CPU利用率"] == pytest.approx(0.8)
    assert result["频率(MHz)"] == pytest.approx(2000)
    assert result["IPC"] == pytest.approx(2.0)
    assert result["Retire"] == pytest.approx(0.5)
    assert result["Retiring Fastpath"] == pytest.approx(0.4)
    assert result["Retiring Microcode"] == pytest.approx(0.1)
    assert result["FrontendBound"] == pytest.approx(0.2)
    assert result["Fetch Latency Bound"] == pytest.approx(0.1)
    assert result["Fetch Bandwidth Bound"] == pytest.approx(0.15)
    assert result["BackendBound"] == pytest.approx(0.4)
    assert result["Backend Memory (estimate)"] == pytest.approx(0.3)
    assert result["Backend CPU (estimate)"] == pytest.approx(0.2)
    assert result["SMTContention"] == pytest.approx(0.1)
    assert result["BadSpec"] == pytest.approx(1 / 3)
    assert result["BadSpec Mispredict (estimate)"] == pytest.approx(0.25)
    assert result["BadSpec Restart (estimate)"] == pytest.approx(1 / 12)
    assert result["br missrate"] == pytest.approx(0.1)
    assert result["br mpki"] == pytest.approx(40)
    assert result["L2 I+D+prefetch missrate"] == pytest.approx(200 / 1170)
    assert result["L2 I+D+prefetch mpki"] == pytest.approx(50)
    assert result["L1 Bound"] is None and result["L3 missrate"] is None
    # Parent and child rounds must not be silently adjusted to close.
    assert result["Fetch Latency Bound"] + result["Fetch Bandwidth Bound"] != result["FrontendBound"]
    assert result["Backend Memory (estimate)"] + result["Backend CPU (estimate)"] != result["BackendBound"]


def test_frequency_is_weighted_by_same_group_running_time(tmp_path: Path) -> None:
    path = tmp_path / "retiring" / "parsed" / "work.csv"
    path.parent.mkdir(parents=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["valid", "0x76", "time_enabled", "time_running"])
        writer.writeheader()
        writer.writerows([
            {"valid": 1, "0x76": 1000, "time_enabled": 1000, "time_running": 1000},
            {"valid": 1, "0x76": 6000, "time_enabled": 2000, "time_running": 2000},
        ])
    write_rows(tmp_path, "time", {"wall_time_us": 1, "thread_cpu_time_us": 1})
    assert zen4.stage_metrics(tmp_path, "work")["频率(MHz)"] == pytest.approx(7000 / 3000 * 1000)


@pytest.mark.parametrize("bad_row", [
    {"valid": "0", "0x76": "100", "time_enabled": "100", "time_running": "100"},
    {"valid": "1", "0x76": "100", "time_enabled": "100", "time_running": "0"},
    {"valid": "1", "0x76": "100", "time_enabled": "100", "time_running": "99"},
    {"valid": "1", "0x76": "100", "time_enabled": "100"},
    {"valid": "1", "0x76": "100", "time_enabled": "100", "time_running": "invalid"},
    {"valid": "1", "time_enabled": "100", "time_running": "100"},
])
def test_frequency_rejects_bad_selected_samples(bad_row: dict[str, str]) -> None:
    good_row = {"valid": "1", "0x76": "100", "time_enabled": "100", "time_running": "100"}
    assert zen4.frequency_mhz([good_row, bad_row]) is None
    assert zen4.frequency_mhz([]) is None


def test_frequency_does_not_silently_drop_invalid_selected_rows(tmp_path: Path) -> None:
    path = tmp_path / "retiring" / "parsed" / "work.csv"
    path.parent.mkdir(parents=True)
    path.write_text("valid,0x76,time_enabled,time_running\n1,100,100,100\n0,200,100,100\n")
    assert zen4.stage_metrics(tmp_path, "work")["频率(MHz)"] is None


def test_missing_and_zero_denominators_are_unavailable(tmp_path: Path) -> None:
    write_rows(tmp_path, "slots", {
        "0x76": 0, "0x1000001a0": 8, "0x100001ea0": 9, "0x1000060a0": 1,
    })
    write_rows(tmp_path, "backend", {
        "0x76": 100, "0x100001ea0": 60, "0xa2d6": 0, "0x2d6": 0,
    })
    write_rows(tmp_path, "branch", {"0xc0": 0, "0xc2": 0, "0xc3": 0})
    result = zen4.stage_metrics(tmp_path, "work")
    assert result["FrontendBound"] is None
    assert result["SMTContention"] is None
    assert result["Backend Memory (estimate)"] is None
    assert result["br missrate"] is None and result["br mpki"] is None
    assert result["IPC"] is None
    assert result["L2 I+D+prefetch missrate"] is None


def test_observed_ratios_are_not_clipped(tmp_path: Path) -> None:
    write_rows(tmp_path, "slots", {
        "0x76": 100, "0x1000001a0": 900, "0x100001ea0": 60,
        "0x1000060a0": 0,
    })
    write_rows(tmp_path, "speculation", {
        "0x76": 100, "0x7aa": 300, "0xc1": 420, "0xc3": 1, "0x96": 1,
    })
    result = zen4.stage_metrics(tmp_path, "work")
    assert result["FrontendBound"] == pytest.approx(1.5)
    assert result["BadSpec"] == pytest.approx(-0.2)


def test_profile_platform_guard() -> None:
    validate_platform("920b", "Linux", "aarch64", "")
    validate_platform("zen4", "Linux", "x86_64", CPUINFO)
    with pytest.raises(RuntimeError, match="Zen 4"):
        validate_platform("zen4", "Linux", "x86_64", CPUINFO.replace("model : 17", "model : 33"))
    with pytest.raises(RuntimeError, match="Zen 4"):
        validate_platform("zen4", "Linux", "x86_64", CPUINFO.replace("AuthenticAMD", "GenuineIntel"))
    with pytest.raises(RuntimeError, match="aarch64"):
        validate_platform("920b", "Linux", "x86_64", CPUINFO)


def test_standalone_collection_and_multiplex_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_read_text = Path.read_text
    multiplex = False

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == Path("/proc/cpuinfo"):
            return CPUINFO
        return original_read_text(path, *args, **kwargs)

    def start(command: list[str], stdout: object, stderr: int, env: dict[str, str]) -> SimpleNamespace:
        identity = Path(env["KPERF_RUNTIME_IDENTITY_DIR"])
        identity.mkdir(parents=True)
        (identity / "123.json").write_text(json.dumps({"pid": 123}), encoding="utf-8")
        if env["KPERF_MODE"] == "time":
            stdout.write("KPERF_TIME,work,1,100000,80000,1\n")
        else:
            events = env["KPERF_RAW_EVENTS"].split(",")
            values = ["100" if event == "0x76" else "10" for event in events]
            running = 99 if multiplex else 100
            stdout.write(f"KPERF,work,1,100,{running},1,{','.join(values)}\n")
        return SimpleNamespace(pid=123, wait=lambda: 0)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr("topdown.runner.platform.system", lambda: "Linux")
    monkeypatch.setattr("topdown.runner.platform.machine", lambda: "x86_64")
    monkeypatch.setattr("topdown.runner.platform.platform", lambda: "test-linux-x86_64")
    monkeypatch.setattr("topdown.runner.os.sched_getaffinity", lambda pid: {0}, raising=False)
    monkeypatch.setattr("topdown.runner.subprocess.Popen", start)
    root = tmp_path / "success"
    result = collect(["python", "workload.py"], root, "zen4", 0, 1)
    assert result["status"] == "complete" and len(result["rounds"]) == 9
    assert "不同轮次" in result["metric_semantics"]
    with (root / "collection_quality.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.DictReader(stream))) == 9
    book = load_workbook(root / "Topdown.xlsx")
    sheet = book["Topdown"]
    assert sheet.freeze_panes == "B2" and sheet["A1"].comment is not None
    assert sheet.sheet_properties.pageSetUpPr.fitToPage and sheet.page_setup.fitToHeight == 1
    labels = {str(row[0].value).lstrip("- "): row[1] for row in sheet.iter_rows(min_row=2)}
    assert labels["SMTContention"].number_format == "0.00%"
    assert labels["L1 Bound"].value == "N/A"
    assert labels[zen4.FREQUENCY_LABEL].value == pytest.approx(1000)
    frequency_comment = sheet.cell(labels[zen4.FREQUENCY_LABEL].row, 1).comment
    assert "time_running" in frequency_comment.text and "纯业务阶段" in frequency_comment.text
    assert result["frequency_semantics"] == zen4.FREQUENCY_NOTE
    assert sheet.cell(labels["Backend Memory (estimate)"].row, 1).alignment.indent == 1
    book.close()

    raw_only = tmp_path / "raw_only"
    raw_result = collect_raw(["python", "workload.py"], raw_only, "zen4", 0, 1)
    assert raw_result["status"] == "collected" and raw_result["stages"] == ["work"]
    assert not (raw_only / "Topdown.xlsx").exists()

    split_raw, split_report = tmp_path / "split_raw", tmp_path / "split_report"
    collect(["python", "workload.py"], split_raw, "zen4", 0, 1,
            analysis_output=split_report)
    assert not (split_raw / "summary.csv").exists()
    assert (split_report / "summary.csv").read_bytes() == (root / "summary.csv").read_bytes()
    assert (split_report / "Topdown.xlsx").is_file()

    multiplex = True
    with pytest.raises(RuntimeError, match="multiplex"):
        collect(["python", "workload.py"], tmp_path / "multiplex", "zen4", 0, 1)
