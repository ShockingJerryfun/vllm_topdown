"""Check the reusable pipeline runner and Topdown output placement."""

import csv
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from analyze.pmu.topdown.vllm.export import export_metrics
from analyze.latency.vllm import export_benchmark
from bench.infer.llm.vllm.compat.compat import prepare_runtime
from collect.pmu.topdown.vllm.results import organize_results
from common.runners import run as pipeline_runner


@pytest.mark.parametrize("fail", [False, True])
def test_pipeline_runs_cleanup_after_collection(tmp_path: Path, monkeypatch, fail: bool) -> None:
    monkeypatch.setattr(pipeline_runner, "ROOT", tmp_path)
    scripts = {}
    for name in ("prepare", "collect", "analyze", "cleanup"):
        path = tmp_path / f"{name}.py"
        failure = "raise RuntimeError('failed')\n" if fail and name == "collect" else ""
        path.write_text(
            "import json, os\nfrom pathlib import Path\n"
            "context=json.loads(Path(os.environ['INFRAPERF_CONTEXT']).read_text())\n"
            f"with (Path(context['root'])/'steps').open('a') as f: f.write('{name}\\n')\n"
            + failure,
            encoding="utf-8",
        )
        scripts[name] = path.name
    config = tmp_path / "pipeline.yaml"
    config.write_text(yaml.safe_dump({"pipeline": scripts}), encoding="utf-8")
    if fail:
        with pytest.raises(subprocess.CalledProcessError):
            pipeline_runner.run(config)
        assert (tmp_path / "steps").read_text().splitlines() == ["prepare", "collect", "cleanup"]
    else:
        pipeline_runner.run(config)
        assert (tmp_path / "steps").read_text().splitlines() == [
            "prepare", "collect", "analyze", "cleanup"
        ]
    assert not list((tmp_path / "results/.runtime").iterdir())


def test_legacy_runtime_has_all_scripts_and_only_vllm_overrides(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    prepare_runtime(runtime)
    layout = json.loads((Path(__file__).resolve().parents[3] /
                         "bench/infer/llm/vllm/compat/compat_layout.json").read_text())
    assert all((runtime / name).is_file() for name in layout)
    assert len(list((runtime / "vllm").rglob("*.py"))) == 7


def test_pipeline_runs_package_named_steps(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_runner, "ROOT", tmp_path)
    package = tmp_path / "collect/example"
    package.mkdir(parents=True)
    (package / "value.py").write_text("VALUE = 'collected'\n", encoding="utf-8")
    (package / "collect.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "from collect.example.value import VALUE\n"
        "root = Path(json.loads(Path(os.environ['INFRAPERF_CONTEXT']).read_text())['root'])\n"
        "(root / 'value').write_text(VALUE)\n", encoding="utf-8",
    )
    for step in ("prepare", "analyze", "cleanup"):
        (tmp_path / f"{step}.py").write_text("pass\n", encoding="utf-8")
    config = tmp_path / "pipeline.yaml"
    config.write_text(yaml.safe_dump({"pipeline": {
        "prepare": "prepare.py", "collect": "collect/example/collect.py",
        "analyze": "analyze.py", "cleanup": "cleanup.py",
    }}), encoding="utf-8")
    pipeline_runner.run(config)
    assert (tmp_path / "value").read_text() == "collected"


def test_results_and_metric_exports_keep_round_sources(tmp_path: Path) -> None:
    raw = tmp_path / "results/collect/pmu/r1"
    for name in ("time", "end_to_end/time", "benchmark_after", "topdown"):
        folder = raw / name
        folder.mkdir(parents=True)
        (folder / "record").write_text(name, encoding="utf-8")
    organize_results(tmp_path, "r1")
    assert (raw / "time/record").read_text() == "time"
    assert (tmp_path / "results/collect/time/r1/end_to_end/time/record").is_file()
    assert (tmp_path / "results/bench/infer/llm/r1/benchmark_after/record").is_file()
    assert (raw / "topdown/record").is_file()

    result = tmp_path / "analysis"
    result.mkdir()
    (result / "summary.csv").write_text(
        ",parent,decode\nIPC,1.25,2.5\nBackendBound,25.0%,未采集\n",
        encoding="utf-8-sig",
    )
    export_metrics(result, "r1", "920b")
    with (result / "metrics.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert rows[2]["value"] == "25.0" and rows[2]["unit"] == "pct"
    assert rows[3]["value"] == "" and rows[3]["status"] == "not_collected"


def test_benchmark_export_preserves_rounds_and_colored_output(tmp_path: Path) -> None:
    bench = tmp_path / "bench"
    for phase, value in (("benchmark_before", "45.2"), ("benchmark_after", "49.8")):
        path = bench / phase
        path.mkdir(parents=True)
        (path / "benchmark.log").write_text(
            "============ Serving Benchmark Result ============\n"
            f"\x1b[32mOutput token throughput (tok/s): {value}\x1b[0m\n"
            "==================================================\n",
            encoding="utf-8",
        )
    export_benchmark(bench, tmp_path / "output")
    with (tmp_path / "output/benchmark.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [(row["round"], row["value"]) for row in rows] == [
        ("benchmark_before", "45.2"), ("benchmark_after", "49.8")
    ]
