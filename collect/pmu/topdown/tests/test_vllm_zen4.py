"""Exercise Zen 4 scheduling and reporting without a vLLM server or GPU."""

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

from adapters.vllm.scripts import pmu_identity, switch_pmu, validate_benchmark
from adapters.vllm.scripts.build_xlsx import ZEN4_SUMMARY_METRICS, load_config
from topdown.profiles import zen4
from topdown.runner import load_groups


@pytest.fixture
def adapter_workspace(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    source = Path(__file__).resolve().parents[1]
    workspace = tmp_path / "workspace"
    adapter = workspace / "adapters/vllm"
    adapter.parent.mkdir(parents=True)
    shutil.copytree(source / "adapters/vllm", adapter)
    (workspace / "topdown").symlink_to(source / "topdown", target_is_directory=True)
    runner = adapter / "scripts/run_one.sh"
    runner.write_text(
        """#!/usr/bin/env bash
set -eu
if [[ "$1" == service ]]; then
    trap 'exit 0' TERM
    while :; do sleep 0.1; done
fi
exec "$MOCK_PYTHON" "$MOCK_ROUND" "$@"
"""
    )
    runner.chmod(0o755)
    tools = tmp_path / "bin"
    tools.mkdir()
    python = tools / "python"
    python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'pmu_identity.py zen4'*) "
        "echo '{\"profile\":\"zen4\",\"model\":\"17\"}'; exit 0;;\n"
        "  *'import os, sys, torch, vllm'*) exit 0;;\n"
        "esac\n"
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
        "TOPDOWN_PMU_PROFILE=zen4\n"
        "ANON_THP_MODE=on\nRANDOM_OUTPUT_LEN=3\nCOLLECTION_PROFILE=full\n"
    )
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "ROUND_RECORD": str(tmp_path / "rounds.jsonl"),
        "MOCK_PYTHON": sys.executable,
        "MOCK_ROUND": str(source / "tests/fixtures/fake_vllm_round.py"),
    }
    env.pop("CODE_PAGE_CONDITION", None)
    return adapter, config, env


def execute_adapter(
    adapter: Path, config: Path, env: dict[str, str], root: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(adapter / "run.sh"), str(config), str(root)],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_baseline_configuration_disables_async_and_spe() -> None:
    source = Path(__file__).resolve().parents[1] / "adapters/vllm"
    script = (
        'set -a; source "$1/scripts/config.env"; '
        'printf "%s\\n" "$VLLM_USE_V2_MODEL_RUNNER" "$SERVER_FLAGS" '
        '"$RANDOM_INPUT_LEN" "$RANDOM_OUTPUT_LEN" "$NUM_PROMPTS" '
        '"$MAX_CONCURRENCY" "$TENSOR_PARALLEL_SIZE" "$DATA_PARALLEL_SIZE" '
        '"$COLLECTION_PROFILE" "$SPE_ENABLE"'
    )
    env = {"PATH": os.environ["PATH"], "TOPDOWN_CONFIG": str(source / "config.example.env")}
    result = subprocess.run(
        ["bash", "-c", script, "_", str(source)], env=env,
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.splitlines() == [
        "1", "--no-async-scheduling", "7000", "100", "1", "1", "1", "1", "full", "0"
    ]


def test_zen4_platform_identity_validates_cpu_family() -> None:
    cpuinfo = "vendor_id\t: AuthenticAMD\ncpu family\t: 25\nmodel\t: 17\n"
    assert pmu_identity.identity("zen4", "Linux", "x86_64", cpuinfo) == {
        "profile": "zen4", "system": "Linux", "machine": "x86_64",
        "vendor": "AuthenticAMD", "family": "25", "model": "17",
    }
    with pytest.raises(RuntimeError, match="Zen 4"):
        pmu_identity.identity("zen4", "Linux", "x86_64", cpuinfo.replace("25", "23"))


def test_benchmark_validation_rejects_incomplete_requests(tmp_path: Path) -> None:
    log = tmp_path / "benchmark.log"
    log.write_text("Serving Benchmark Result\nSuccessful requests: 1\nFailed requests: 0\n")
    client = tmp_path / "benchmark.client.json"
    client.write_text(json.dumps({"status": "ok", "result": {"completed": 1, "failed": 0}}))
    assert validate_benchmark.validate(log, 1) == {"expected": 1, "completed": 1, "failed": 0}
    client.write_text(json.dumps({"status": "ok", "result": {"completed": 0, "failed": 1}}))
    with pytest.raises(ValueError, match="incomplete"):
        validate_benchmark.validate(log, 1)
    client.unlink()
    log.write_text("Serving Benchmark Result\nSuccessful requests: 0\n")
    with pytest.raises(ValueError, match="completed 0 of 1"):
        validate_benchmark.validate(log, 1)
    log.write_text("Serving Benchmark Result\nSuccessful requests: 1\nFailed requests: 1\n")
    with pytest.raises(ValueError, match="failed request count"):
        validate_benchmark.validate(log, 1)


def test_zen4_end_to_end_scheduler_parser_and_workbook(
    adapter_workspace: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    adapter, config, env = adapter_workspace
    root = tmp_path / "results"
    output = execute_adapter(adapter, config, env, root)
    assert output.returncode == 0, output.stderr
    groups = load_groups("zen4")
    report_groups = load_config(adapter / "scripts/zen4/report_config.json")
    assert {group.name: list(group.events) for group in report_groups} == groups
    expected_metrics = list(zen4.stage_metrics(tmp_path, "uncollected"))
    expected_metrics.insert(expected_metrics.index("cycles") + 1, "cycle占比")
    assert tuple(expected_metrics) == ZEN4_SUMMARY_METRICS

    records = [json.loads(line) for line in Path(env["ROUND_RECORD"]).read_text().splitlines()]
    pipeline = [row for row in records if row["label"] in groups and not row["target"]]
    parent = [row for row in records if row["label"] in groups and row["target"]]
    assert [(row["label"], row["codes"]) for row in pipeline] == list(groups.items())
    assert [(row["label"], row["codes"]) for row in parent] == list(groups.items())
    assert all(row["target"] == "execute_model_to_sample_tokens"
               and row["qualifier"] == "run_fullgraph" for row in parent)
    assert records[0]["label"] == "benchmark_before"
    assert records[-1]["label"] == "benchmark_after"
    assert [row["label"] for row in records].count("hotspot") == 1
    assert (root / "pmu_profile").read_text().strip() == "zen4"
    assert json.loads((root / "pmu_identity.json").read_text())["model"] == "17"
    commands = (root / "commands.txt").read_text()
    assert "--require-unmultiplexed" in commands and "--pmu-profile zen4" in commands

    with (root / "collection_quality.csv").open(encoding="utf-8-sig", newline="") as stream:
        quality = list(csv.DictReader(stream))
    assert len(quality) == 81
    assert all(row["status"] == "ok" and row["selected"] == "2" for row in quality)
    assert (root / "details/topdown/end_to_end/slots/parsed/execute_model_to_sample_tokens.csv").is_file()
    assert (root / "details/topdown/slots/parsed/run_fullgraph.csv").is_file()

    book = load_workbook(root / "zen4_vllm0.26_qwen3_7k3.xlsx")
    assert book.sheetnames == ["Topdown", "Hotspot"]
    sheet = book["Topdown"]
    assert sheet.freeze_panes == "B2" and sheet["A1"].comment is not None
    assert sheet["B1"].value == "execute_model_to_sample_tokens" and sheet.max_column == 10
    rows = {str(row[0].value).lstrip("- "): row for row in sheet.iter_rows(min_row=2)}
    assert rows["FrontendBound"][1].value == pytest.approx(0.15)
    assert rows["BackendBound"][1].value == pytest.approx(0.20)
    assert rows["SMTContention"][1].value == pytest.approx(0.01)
    assert rows["BadSpec"][1].value == pytest.approx(0.10)
    assert rows["L2 I+D+prefetch missrate"][1].value == pytest.approx(0.1709)
    assert rows["L2 I+D+prefetch mpki"][1].value == pytest.approx(80.0)
    assert rows["br missrate"][1].value == pytest.approx(0.05)
    assert rows["L1 Bound"][1].value == "N/A"
    assert rows[zen4.FREQUENCY_LABEL][1].value == pytest.approx(1000)
    assert "time_running" in rows[zen4.FREQUENCY_LABEL][0].comment.text
    assert rows["cycle占比"][1].value == "不适用"
    assert rows["cycle占比"][2].value == pytest.approx(0.125)
    assert "不同轮次" in rows["Backend Memory (estimate)"][0].comment.text
    assert "Benchmark" in rows and "Mean TTFT (ms)" in rows
    assert book["Hotspot"]["D2"].value == "[.] fixture_function"
    assert "0 Ports Serialize" not in rows and "L2 LSU Hit Rate" not in rows
    book.close()


def test_zen4_scheduler_rejects_multiplexed_rows(
    adapter_workspace: tuple[Path, Path, dict[str, str]], tmp_path: Path
) -> None:
    adapter, config, env = adapter_workspace
    env["FAKE_MULTIPLEX"] = "1"
    root = tmp_path / "failed"
    output = execute_adapter(adapter, config, env, root)
    assert output.returncode != 0 and "invalid_rows" in output.stderr
    assert not list(root.glob("*.xlsx"))


@pytest.mark.parametrize("setting", ["ZERO_PORT_HOTSPOT=1", "SPE_ENABLE=1", "FREQUENCY_ENABLE=1"])
def test_zen4_rejects_920b_specific_collection_options(
    adapter_workspace: tuple[Path, Path, dict[str, str]], tmp_path: Path, setting: str
) -> None:
    adapter, config, env = adapter_workspace
    with config.open("a", encoding="utf-8") as stream:
        stream.write(setting + "\n")
    root = tmp_path / "rejected"
    output = execute_adapter(adapter, config, env, root)
    assert output.returncode != 0 and "does not support" in output.stderr
    assert not root.exists()


def test_switch_rpc_preserves_complete_zen4_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests = []
    corrupt = False

    def post(url: str, payload: dict[str, object], timeout: float) -> dict[str, object]:
        requests.append((url, payload))
        if url.endswith("collective_rpc"):
            kwargs = payload["kwargs"]
            events = [int(value, 0) for value in kwargs["codes"].split(",")]
            if corrupt:
                events[0] += 1
            return {"results": [{"round_id": kwargs["round_id"], "mode": kwargs["mode"],
                                 "events": events,
                                 "names": kwargs["names"].split(","), "pid": 41, "tid": 41}]}
        return {}

    monkeypatch.setattr(switch_pmu, "post", post)
    monkeypatch.setenv("KPERF_MODE", "pmu")
    for label, events in load_groups("zen4").items():
        monkeypatch.setenv("KPERF_RAW_EVENTS", ",".join(events))
        monkeypatch.setenv("KPERF_EVENT_NAMES", ",".join(events))
        result = switch_pmu.switch("http://127.0.0.1:1", label, tmp_path / "worker.json", False, 1)
        assert result["events"] == [int(event, 0) for event in events]
    rpc = [payload for url, payload in requests if url.endswith("collective_rpc")]
    assert len(rpc) == 8
    assert "0x1060001a0" in rpc[2]["kwargs"]["codes"]
    corrupt = True
    with pytest.raises(RuntimeError, match="different event group"):
        switch_pmu.switch("http://127.0.0.1:1", "mismatch", tmp_path / "worker.json", False, 1)
