#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Summarize explicitly accepted runs of the staged Topdown comparison."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import runpy
import shutil
import statistics
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

if __package__:
    from .spe import report
    from .spe.compact import verify_retention
else:
    from spe import report
    from spe.compact import verify_retention

LOGGER = logging.getLogger(__name__)
TASK_ROOT = Path(__file__).resolve().parent
COLLECTOR = (
    TASK_ROOT.parent
    if (TASK_ROOT / "build_xlsx.py").is_file()
    else TASK_ROOT.parents[1] / "vllm_fj"
)
STAGE = "execute_model_to_sample_tokens"
FACTORS = ("script", "cores", "page_mode", "locality")
ROLE_FACTORS = {
    "cores": "cores",
    "pages": "page_mode",
    "locality": "locality",
    "scripts": "script",
}
SHEET_NAMES = ("逐次对比", "重复汇总", "报告索引")
SUMMARY_METRICS = {
    "time(us)": ("e2e_wall_us", "端到端 wall 时间", "us"),
    "cycles": ("cycles", "cycles", "cycles"),
    "instructions": ("instructions", "instructions", "instructions"),
    "IPC": ("ipc", "IPC", "instructions/cycle"),
    "Retire": ("retire_pct", "Retire", "%"),
    "FrontendBound": ("frontend_pct", "FrontendBound", "%"),
    "BadSpec": ("badspec_pct", "BadSpec", "%"),
    "BackendBound": ("backend_pct", "BackendBound", "%"),
}
BENCHMARK_METRICS = {
    "Mean TTFT (ms)": ("mean_ttft_ms", "客户端 Mean TTFT", "ms"),
    "Mean TPOT (ms)": ("mean_tpot_ms", "客户端 Mean TPOT", "ms"),
    "Output token throughput (tok/s)": ("output_tok_s", "输出 token 吞吐", "tok/s"),
    "Total token throughput (tok/s)": ("total_tok_s", "总 token 吞吐", "tok/s"),
    "Request throughput (req/s)": ("request_s", "请求吞吐", "req/s"),
    "Benchmark duration (s)": ("benchmark_s", "客户端 benchmark 时长", "s"),
}
SEMANTICS = [
    "page_mode 标记实际代码 folio 条件，不表示系统基础页大小。",
    "summary.csv 指标原值来自各自独立采集轮次；未用 cycles/time 推导因果或频率。",
    "时间轮内 Decode 调用范围与跨次重复范围分开保存；均不是置信区间。",
    "差值是否超出重复范围仅作描述，不构成显著性或因果证明。",
    "比较范围取候选与基线各自重复极差的较大者；每组至少两次才描述是否超出。",
    "只汇总清单中已通过验收的运行；不表示全部计划阶段已经完成。",
]


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def number(value: object, context: str) -> float:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        raise ValueError(f"missing numeric {context}")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid numeric {context}: {value!r}") from exc
    if not math.isfinite(result):
        raise ValueError(f"nonfinite {context}")
    return result


def integer(value: object, context: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise ValueError(f"missing or noninteger {context}")


def distribution(values: Iterable[float]) -> dict[str, float | int]:
    data = list(values)
    if not data:
        raise ValueError("cannot summarize an empty population")
    return {
        "n": len(data),
        "mean": statistics.mean(data),
        "median": statistics.median(data),
        "min": min(data),
        "max": max(data),
        "range": max(data) - min(data),
    }


def metric(
    value: float, label: str, unit: str, source: str, raw: str | None = None
) -> dict:
    return {"value": value, "label": label, "unit": unit, "source": source, "raw": raw}


def manifest_entries(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest must be a nonempty list of accepted runs")
    identities = set()
    repeats = set()
    result = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("manifest entry must be an object")
        item = dict(item)
        for key in ("run_dir", *FACTORS, "repeat", "role", "baseline"):
            if key not in item:
                raise ValueError(f"missing manifest field {key}")
        if item["role"] not in ROLE_FACTORS or not isinstance(item["baseline"], bool):
            raise ValueError("invalid comparison role or baseline flag")
        if item["script"] not in {"new", "old"} or item["cores"] not in {1, 2, 4}:
            raise ValueError("script must be new/old and cores must be 1/2/4")
        if not isinstance(item["cores"], int) or isinstance(item["cores"], bool):
            raise ValueError("core count must be an integer")
        if item["page_mode"] not in {"4K", "64K"} or item["locality"] not in {
            "near",
            "far",
        }:
            raise ValueError("unsupported code-folio condition or locality")
        if (
            not isinstance(item["repeat"], int)
            or isinstance(item["repeat"], bool)
            or item["repeat"] < 1
        ):
            raise ValueError("repeat must be a positive integer")
        run_dir = Path(item["run_dir"])
        item["run_dir"] = str(
            (path.parent / run_dir).resolve()
            if not run_dir.is_absolute()
            else run_dir.resolve()
        )
        identity = item["role"], item["run_dir"]
        repetition = item["role"], *(item[key] for key in FACTORS), item["repeat"]
        if identity in identities or repetition in repeats:
            raise ValueError(
                "duplicate run or condition/repeat within a comparison role"
            )
        identities.add(identity)
        repeats.add(repetition)
        if item.get("audit_file"):
            audit_path = Path(item["audit_file"])
            item["audit_file"] = str(
                path.parent / audit_path if not audit_path.is_absolute() else audit_path
            )
        result.append(item)
    return result


def verify_spe_independent_checks(audit: dict, spe_root: Path | None = None) -> None:
    """Require applicable per-file checks and a consistent decoder aggregate."""
    if spe_root is not None and (spe_root / "analysis/retention.json").exists():
        receipt = verify_retention(spe_root)
        if any(receipt[key] != audit[key] for key in ("selected_samples", "pc_count")):
            raise ValueError("SPE selected replay counts disagree with decoder audit")
        return
    independent = audit.get("independent_checks")
    if not isinstance(independent, dict):
        raise ValueError("SPE independent checks are missing")
    for kind in ("packet_dump", "perf_memory"):
        aggregate = independent.get(kind)
        if isinstance(aggregate, dict):
            checks = aggregate.get("files")
            if aggregate.get("status") != "pass":
                raise ValueError(f"SPE independent {kind} aggregate did not pass")
        else:
            checks = aggregate  # Historical normalized archives used a list.
        if (
            not isinstance(checks, list)
            or not checks
            or not all(isinstance(check, dict) for check in checks)
            or not any(check.get("status") == "pass" for check in checks)
            or any(
                check.get("status") not in {"pass", "not_applicable"}
                for check in checks
            )
        ):
            raise ValueError(f"SPE independent {kind} audit is incomplete")


def verify_acceptance(entry: dict) -> tuple[dict, dict, Path]:
    root = Path(entry["run_dir"])
    complete = read_json(root / "complete.json")
    started = integer(complete.get("started_ns"), "complete.started_ns")
    finished = integer(complete.get("finished_ns"), "complete.finished_ns")
    if started < 1 or finished <= started or complete.get("gpu_pids") != []:
        raise ValueError(f"incomplete duration or GPU cleanup: {root}")
    audit = read_json(Path(entry.get("audit_file", root / "acceptance.json")))
    if audit.get("status") != "pass" or any(
        audit.get("checks", {}).get(check) != "pass"
        for check in ("runtime", "placement", "pages", "cleanup")
    ):
        raise ValueError(f"run acceptance audit did not pass: {root}")
    scope = audit.get("scope", {})
    if not isinstance(scope, dict):
        raise ValueError("acceptance scope must be an object")
    for factor in FACTORS:
        if factor in scope and scope[factor] != entry[factor]:
            raise ValueError(f"manifest disagrees with accepted {factor}")
    source_name = complete.get("report")
    if not isinstance(source_name, str):
        raise ValueError("completed run did not identify its final workbook")
    workbook = root / Path(source_name).name
    if not workbook.is_file() or not workbook.stat().st_size:
        raise ValueError(f"completed run workbook is missing: {workbook}")
    if not isinstance(complete.get("spe"), bool):
        raise ValueError("completion evidence must state whether SPE was collected")
    if complete["spe"]:
        spe_audit = read_json(root / "spe/analysis/audit.json")
        spe_manifest = read_json(root / "spe/analysis/manifest.json")
        capture = read_json(root / "spe/capture_complete.json")
        if any(value.get("status") != "pass" for value in (spe_audit, spe_manifest)):
            raise ValueError("SPE normalization did not pass")
        if (
            spe_audit.get("selected_samples", 0) <= 0
            or spe_audit.get("pc_count", 0) <= 0
        ):
            raise ValueError("SPE result is empty")
        if any(
            spe_audit.get(key) != spe_manifest.get(key)
            for key in ("selected_samples", "pc_count")
        ):
            raise ValueError("SPE audit and manifest counts disagree")
        if (
            capture.get("owned_worker_released") is not True
            or capture.get("remaining_gpu_pids") != []
            or capture.get("perf_readers_complete") is not True
        ):
            raise ValueError("SPE cleanup was not verified")
        verify_spe_independent_checks(spe_audit, root / "spe")
    return complete, audit, workbook


def verify_quality(root: Path, groups: set[str]) -> None:
    quality = read_csv(root / "collection_quality.csv")
    rows = [row for row in quality if row.get("group", "").startswith("end_to_end/")]
    seen = set()
    for row in rows:
        group = row["group"].split("/", 1)[1]
        if group in seen or row.get("stage") != STAGE:
            raise ValueError("duplicate PMU group or wrong E2E quality stage")
        seen.add(group)
        counts = {
            field: integer(row.get(field), f"{group}.{field}")
            for field in ("expected_selected", "raw", "selected", "valid", "invalid")
        }
        if (
            row.get("status") != "ok"
            or counts["selected"] <= 0
            or counts["invalid"] != 0
            or counts["selected"] != counts["valid"]
            or counts["selected"] != counts["expected_selected"]
            or counts["raw"] < counts["selected"]
        ):
            raise ValueError(f"E2E collection quality failed: {group}")
    if seen != groups | {"time"}:
        raise ValueError("E2E time/PMU collection groups are missing or unexpected")


def load_metrics(root: Path, native: dict) -> tuple[dict, dict]:
    with (root / "summary.csv").open(encoding="utf-8-sig", newline="") as source:
        matrix = list(csv.reader(source))
    if not matrix or matrix[0].count(STAGE) != 1:
        raise ValueError("summary.csv must contain exactly one E2E stage column")
    column = matrix[0].index(STAGE)
    by_name = {}
    for row in matrix[1:]:
        if row and row[0] in SUMMARY_METRICS:
            if row[0] in by_name:
                raise ValueError(f"duplicate summary metric {row[0]}")
            by_name[row[0]] = row[column] if len(row) > column else None
    result = {}
    for label, (key, display, unit) in SUMMARY_METRICS.items():
        raw = by_name.get(label)
        if not isinstance(raw, str) or (unit == "%") != raw.endswith("%"):
            raise ValueError(f"missing or wrong unit for summary metric {label}")
        result[key] = metric(
            number(raw[:-1] if unit == "%" else raw, label),
            display,
            unit,
            "summary.csv",
            raw,
        )
    timing = read_csv(root / "end_to_end/time/parsed" / f"{STAGE}.csv")
    if not timing or any(row.get("valid") != "1" for row in timing):
        raise ValueError("E2E parsed timing rows are empty or invalid")
    if len({row.get("global_call") for row in timing}) != len(timing):
        raise ValueError("duplicate aligned timing calls")
    spread = {
        field: distribution(number(row.get(field), field) for row in timing)
        for field in ("wall_time_us", "thread_cpu_time_us")
    }
    if abs(spread["wall_time_us"]["mean"] - result["e2e_wall_us"]["value"]) > 0.005001:
        raise ValueError("E2E summary wall time disagrees with aligned timing rows")
    result["thread_cpu_us"] = metric(
        spread["thread_cpu_time_us"]["mean"],
        "端到端线程 CPU 时间",
        "us",
        f"end_to_end/time/parsed/{STAGE}.csv",
    )
    pairs = native["read_time_benchmark"](root / "end_to_end")
    benchmark = dict(pairs)
    if len(benchmark) != len(pairs):
        raise ValueError("duplicate native benchmark result labels")
    if (
        number(benchmark.get("Successful requests"), "successful requests") <= 0
        or number(benchmark.get("Failed requests"), "failed requests") != 0
    ):
        raise ValueError("client benchmark requests failed or did not complete")
    for label, (key, display, unit) in BENCHMARK_METRICS.items():
        raw = benchmark.get(label)
        result[key] = metric(
            number(raw, label), display, unit, "end_to_end/time/benchmark.log", raw
        )
    return result, spread


def load_run(entry: dict, native: dict, groups: set[str]) -> dict:
    complete, audit, workbook = verify_acceptance(entry)
    root = Path(entry["run_dir"])
    verify_quality(root, groups)
    metrics, spread = load_metrics(root, native)
    # Integer subtraction retains nanosecond precision before converting to seconds.
    duration = (int(complete["finished_ns"]) - int(complete["started_ns"])) / 1e9
    metrics["experiment_s"] = metric(duration, "完整采集耗时", "s", "complete.json")
    return {
        **entry,
        "run_id": root.name,
        "workbook": str(workbook),
        "spe": complete["spe"],
        "scope": audit.get("scope", {}),
        "metrics": metrics,
        "within_time_round": spread,
        "started_ns": str(complete["started_ns"]),
        "finished_ns": str(complete["finished_ns"]),
    }


def condition(run: dict) -> tuple:
    return tuple(run[key] for key in FACTORS)


def validate_roles(runs: list[dict]) -> None:
    by_role = defaultdict(list)
    for run in runs:
        by_role[run["role"]].append(run)
    for role, members in by_role.items():
        baselines = {condition(run) for run in members if run["baseline"]}
        if len(baselines) != 1:
            raise ValueError(f"role {role} must have exactly one baseline condition")
        for factor in FACTORS:
            if (
                factor != ROLE_FACTORS[role]
                and len({run[factor] for run in members}) != 1
            ):
                raise ValueError(f"role {role} changed uncontrolled factor {factor}")
        if role == "cores" and any(
            (run["script"], run["page_mode"], run["locality"]) != ("new", "64K", "near")
            for run in members
        ):
            raise ValueError(
                "core comparison requires new script / 64K code / near GPU"
            )
        if role != "cores" and len({run["spe"] for run in members}) != 1:
            raise ValueError("SPE collection differs within a comparison role")
        for key in (
            "model",
            "input_len",
            "output_len",
            "execution_mode",
            "event_config_hash",
            "window",
        ):
            values = [run["scope"].get(key) for run in members]
            if any(value is not None for value in values) and (
                any(value is None for value in values)
                or len({json.dumps(value, sort_keys=True) for value in values}) != 1
            ):
                raise ValueError(f"measurement contract differs within {role}: {key}")
        for group in {condition(run) for run in members}:
            if (
                len({run["baseline"] for run in members if condition(run) == group})
                != 1
            ):
                raise ValueError(
                    "baseline flag must be consistent across condition repeats"
                )


def aggregate_runs(runs: list[dict]) -> list[dict]:
    groups = defaultdict(list)
    for run in runs:
        groups[(run["role"], *condition(run))].append(run)
    stats = {}
    for key, members in groups.items():
        stats[key] = {
            metric_key: distribution(
                run["metrics"][metric_key]["value"] for run in members
            )
            for metric_key in members[0]["metrics"]
        }
    baselines = {
        run["role"]: (run["role"], *condition(run)) for run in runs if run["baseline"]
    }
    rows = []
    for key, members in groups.items():
        first = members[0]
        for metric_key, current in stats[key].items():
            baseline = stats[baselines[first["role"]]][metric_key]
            delta = current["median"] - baseline["median"]
            relative = 100 * delta / baseline["median"] if baseline["median"] else None
            variation = max(current["range"], baseline["range"])
            if first["baseline"]:
                description = "基线"
            elif min(current["n"], baseline["n"]) < 2:
                description = "重复不足两次，未判断"
            elif abs(delta) > variation:
                description = "差值超出本组重复范围"
            else:
                description = "差值未超出本组重复范围"
            source = first["metrics"][metric_key]
            rows.append(
                {
                    "role": first["role"],
                    **dict(zip(FACTORS, condition(first), strict=True)),
                    "baseline": first["baseline"],
                    "metric": metric_key,
                    "label": source["label"],
                    "unit": source["unit"],
                    **current,
                    "baseline_n": baseline["n"],
                    "baseline_median": baseline["median"],
                    "baseline_min": baseline["min"],
                    "baseline_max": baseline["max"],
                    "delta_absolute": delta,
                    "delta_relative_pct": relative,
                    "relative_status": "defined"
                    if relative is not None
                    else "undefined_zero_baseline",
                    "delta_unit": "百分点" if source["unit"] == "%" else source["unit"],
                    "variation_reference": variation,
                    "variation_description": description,
                }
            )
    return rows


def core_suggestion(aggregates: list[dict]) -> dict:
    groups = defaultdict(dict)
    for row in aggregates:
        if row["role"] == "cores":
            groups[row["cores"]][row["metric"]] = row
    available = sorted(groups)
    result = {
        "available_cores": available,
        "coverage": "all_1_2_4" if available == [1, 2, 4] else "partial",
        "suggested_cores": None,
        "selection_metric": "e2e_wall_us",
        "decisions": [],
    }
    if not available or any(group["e2e_wall_us"]["n"] < 2 for group in groups.values()):
        result["reason"] = "可用条件不足或每个条件尚未完成至少两次重复。"
        return result
    selected = available[0]
    for cores in available[1:]:
        candidate, current = groups[cores], groups[selected]
        faster = current["e2e_wall_us"]["median"] - candidate["e2e_wall_us"][
            "median"
        ] > max(current["e2e_wall_us"]["range"], candidate["e2e_wall_us"]["range"])
        regressions = [
            key
            for key in ("mean_ttft_ms", "mean_tpot_ms")
            if candidate[key]["median"] - current[key]["median"]
            > max(candidate[key]["range"], current[key]["range"])
        ]
        result["decisions"].append(
            {
                "cores": cores,
                "reference_cores": selected,
                "host_wall_improvement_exceeds_repeat_range": faster,
                "client_regressions_exceeding_repeat_range": regressions,
            }
        )
        if faster and not regressions:
            selected = cores
    result["suggested_cores"] = selected
    result["reason"] = (
        "仅当端到端 wall 时间改善超出两组重复范围，且客户端 TTFT/TPOT "
        "无超出范围的退化时增加核数；这是描述性建议。"
    )
    return result


def summarize(manifest: Path, collector: Path = COLLECTOR) -> dict:
    native = runpy.run_path(str(collector / "scripts/build_xlsx.py"))
    config = read_json(collector / "scripts/920b/report_config.json")
    groups = {group["name"] for group in config["groups"]}
    runs = [load_run(entry, native, groups) for entry in manifest_entries(manifest)]
    validate_roles(runs)
    aggregates = aggregate_runs(runs)
    return {
        "schema_version": 1,
        "status": "summarized_accepted_inputs",
        "semantics": SEMANTICS,
        "runs": runs,
        "aggregates": aggregates,
        "core_selection": core_suggestion(aggregates),
    }


def run_rows(result: dict) -> list[dict]:
    rows = []
    for run in result["runs"]:
        for key, value in run["metrics"].items():
            timing = run["within_time_round"].get(
                {
                    "e2e_wall_us": "wall_time_us",
                    "thread_cpu_us": "thread_cpu_time_us",
                }.get(key),
                {},
            )
            rows.append(
                {
                    "role": run["role"],
                    "run_id": run["run_id"],
                    **{field: run[field] for field in FACTORS},
                    "repeat": run["repeat"],
                    "baseline": run["baseline"],
                    "metric": key,
                    **value,
                    "time_round_n": timing.get("n"),
                    "time_round_min": timing.get("min"),
                    "time_round_median": timing.get("median"),
                    "time_round_max": timing.get("max"),
                }
            )
    return rows


def save_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def display_value(row: dict, key: str) -> report.CellValue:
    value = row.get(key)
    labels = {
        "role": {
            "cores": "核数",
            "pages": "代码页",
            "locality": "近远端",
            "scripts": "新旧脚本",
        },
        "script": {"new": "常驻", "old": "逐轮重启"},
        "locality": {"near": "近端", "far": "远端"},
        "relative_status": {
            "defined": "已定义",
            "undefined_zero_baseline": "基线为0，未定义",
        },
    }
    if key in labels:
        return labels[key].get(value, value)
    return int(value) if isinstance(value, bool) else value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def report_bundle(result: dict, baseline_run: Path, destination: Path) -> dict:
    """Identify physical reports before copying or publishing any of them."""
    sweep = result.get("sweep")
    if "sweep" in result and (
        not isinstance(sweep, dict)
        or sweep.get("status") != "all_complete"
        or type(sweep.get("expected_physical_runs")) is not int
        or sweep["expected_physical_runs"] <= 0
        or type(sweep.get("accepted_physical_runs")) is not int
        or sweep.get("accepted_physical_runs") != sweep.get("expected_physical_runs")
        or not isinstance(sweep.get("phases"), dict)
        or any(
            sweep.get("phases", {}).get(phase) != "complete"
            for phase in ("cores", "comparisons")
        )
    ):
        raise ValueError("cannot publish an incomplete sweep")
    physical = {}
    names = set()
    baselines = set()
    reserved = {"con", "prn", "aux", "nul"} | {
        f"{prefix}{index}" for prefix in ("com", "lpt") for index in range(1, 10)
    }
    for run in result["runs"]:
        root = Path(run["run_dir"]).resolve()
        run_id = run["run_id"]
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", run_id)
            or run_id.casefold() in reserved
            or run_id != root.name
        ):
            raise ValueError("unsafe or ambiguous report run ID")
        _, _, accepted = verify_acceptance(run)
        source = Path(run["workbook"]).resolve()
        if accepted.resolve() != source:
            raise ValueError("report no longer matches the accepted workbook")
        if destination.resolve() == source:
            raise ValueError("comparison workbook must be a copy of the run workbook")
        identity = {
            "run_id": run_id,
            "run_dir": str(root),
            "source_workbook": str(source),
            **{factor: run[factor] for factor in FACTORS},
            "repeat": run["repeat"],
        }
        if run["baseline"]:
            baselines.add(root)
        if root in physical:
            previous = physical[root]
            if any(previous[key] != value for key, value in identity.items()):
                raise ValueError("conflicting identities for one physical run")
            if run["role"] in previous["roles"]:
                raise ValueError("duplicate comparison role for one physical run")
            previous["roles"].append(run["role"])
            continue
        if run_id.casefold() in names:
            raise ValueError("colliding report run IDs")
        names.add(run_id.casefold())
        with ZipFile(source) as package:
            sheets = ET.fromstring(package.read("xl/workbook.xml")).find(
                "s:sheets", report.NS
            )
            if sheets is None or any(
                sheet.get("name") in SHEET_NAMES for sheet in sheets
            ):
                raise ValueError(
                    "expected an original run workbook without comparisons"
                )
            sheet_names = [sheet.get("name") for sheet in sheets]
        physical[root] = {
            **identity,
            "roles": [run["role"]],
            "relative_path": f"reports/{run_id}.xlsx",
            "sha256": file_sha256(source),
            "sheet_count": len(sheet_names),
            "includes_spe": all(name in sheet_names for name in report.SHEET_NAMES),
            "detail_source": root == baseline_run.resolve(),
        }
    baseline = baseline_run.resolve()
    if baseline not in physical or baseline not in baselines:
        raise ValueError(
            "workbook source must be an explicit baseline run in the manifest"
        )
    rows = sorted(physical.values(), key=lambda row: row["run_id"])
    if "sweep" in result and len(rows) != sweep["expected_physical_runs"]:
        raise ValueError("sweep physical report count disagrees with completion")
    for row in rows:
        row["roles"].sort()
    return {
        "path_base": "workbook_directory",
        "workbook": destination.name,
        "detail_source_run_id": physical[baseline]["run_id"],
        "reports": rows,
    }


def report_index_rows(bundle: dict) -> list[dict]:
    rows = []
    for record in bundle["reports"]:
        count = record["sheet_count"]
        scope = (
            f"本工作簿原有{count}表"
            + ("及SPE明细" if record["includes_spe"] else "")
            + "仅来源此运行"
            if record["detail_source"]
            else f"此运行完整{count}表报告"
        )
        rows.append(
            {
                **record,
                "scope": scope,
                "roles_label": "、".join(
                    str(display_value({"role": role}, "role"))
                    for role in record["roles"]
                ),
                "link": "打开此运行完整报告",
            }
        )
    return rows


def write_report_links(sheet: Path, rows: list[dict], column: int) -> Path:
    hyperlinks = []
    relationships = []
    for index, row in enumerate(rows, 1):
        relationship = f"rIdReport{index}"
        hyperlinks.append(
            f'<hyperlink xmlns:r="{report.REL_NS}" '
            f'ref="{report.column_letter(column)}{index + 1}" '
            f'r:id="{relationship}"/>'
        )
        relationships.append(
            f'<Relationship Id="{relationship}" Type="{report.REL_NS}/hyperlink" '
            f'Target={quoteattr(row["relative_path"])} TargetMode="External"/>'
        )
    content = sheet.read_text(encoding="utf-8")
    sheet.write_text(
        content.replace(
            "<pageMargins",
            "<hyperlinks>" + "".join(hyperlinks) + "</hyperlinks><pageMargins",
            1,
        ),
        encoding="utf-8",
    )
    path = sheet.with_suffix(".rels")
    path.write_text(
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(relationships)
        + "</Relationships>",
        encoding="utf-8",
    )
    return path


def comparison_workbook(result: dict, baseline_run: Path, destination: Path) -> None:
    bundle = report_bundle(result, baseline_run, destination)
    source_record = next(row for row in bundle["reports"] if row["detail_source"])
    report_directory = destination.parent / "reports"
    expected = {Path(row["relative_path"]).name for row in bundle["reports"]}
    if report_directory.is_symlink():
        raise ValueError("report destination cannot be a symbolic link")
    if report_directory.exists():
        if (
            not report_directory.is_dir()
            or {path.name for path in report_directory.iterdir()} != expected
        ):
            raise ValueError("existing reports directory conflicts with this bundle")
        for row in bundle["reports"]:
            target = destination.parent / row["relative_path"]
            if (
                target.is_symlink()
                or not target.is_file()
                or file_sha256(target) != row["sha256"]
            ):
                raise ValueError("existing report differs from the accepted source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    common = (
        report.Column("role", "比较阶段", 14),
        report.Column("script", "脚本", 16),
        report.Column("cores", "Worker 核数", 14),
        report.Column("page_mode", "代码 folio", 14),
        report.Column("locality", "GPU 近远端", 14),
        report.Column("label", "指标", 30),
        report.Column("unit", "单位", 20),
    )
    run_columns = (
        report.Column("run_id", "运行", 30),
        *common,
        report.Column("repeat", "重复序号", 12),
        report.Column("baseline", "基线", 10),
        report.Column("value", "原指标值", 18),
        report.Column("time_round_n", "时间轮调用数", 16),
        report.Column("time_round_min", "时间轮内最小", 18),
        report.Column("time_round_median", "时间轮内中位数", 18),
        report.Column("time_round_max", "时间轮内最大", 18),
    )
    summary_columns = (
        *common,
        report.Column("n", "重复次数", 12),
        report.Column("median", "重复中位数", 18),
        report.Column("min", "重复最小", 18),
        report.Column("max", "重复最大", 18),
        report.Column("range", "重复极差", 18),
        report.Column("baseline_median", "基线中位数", 18),
        report.Column("delta_absolute", "与基线绝对差", 18),
        report.Column("delta_unit", "差值单位", 18),
        report.Column("delta_relative_pct", "与基线相对差 %", 20),
        report.Column("relative_status", "相对差口径", 30),
        report.Column("variation_description", "与重复范围比较", 36),
    )
    index_columns = (
        report.Column("scope", "明细来源与范围", 64),
        report.Column("run_id", "运行ID", 44),
        *common[1:5],
        report.Column("repeat", "重复序号", 12),
        report.Column("roles_label", "比较阶段", 26),
        report.Column("link", "逐次完整报告（相对链接）", 30),
    )
    index_rows = report_index_rows(bundle)
    tables = (run_rows(result), result["aggregates"], index_rows)
    with tempfile.TemporaryDirectory(
        prefix=".comparison_", dir=destination.parent
    ) as temporary:
        directory = Path(temporary)
        staged_reports = directory / "reports"
        staged_reports.mkdir()
        for row in bundle["reports"]:
            copied = directory / row["relative_path"]
            shutil.copyfile(row["source_workbook"], copied)
            if file_sha256(copied) != row["sha256"]:
                raise ValueError("source report changed while copying")
        staged_source = directory / source_record["relative_path"]
        with ZipFile(staged_source) as source:
            styles = report.inherited_styles(source)
        paths = []
        for index, (columns, rows) in enumerate(
            zip((run_columns, summary_columns, index_columns), tables, strict=True)
        ):
            path = directory / f"sheet{index}.xml"
            values = [
                [display_value(row, column.key) for column in columns] for row in rows
            ]
            report.write_sheet(path, columns, values, len(rows), styles)
            paths.append(path)
        links = write_report_links(paths[-1], index_rows, len(index_columns))
        append_comparison_package(
            staged_source, directory / "comparison.xlsx", paths, links
        )
        if not report_directory.exists():
            staged_reports.replace(report_directory)
        (directory / "comparison.xlsx").replace(destination)
    result["report_bundle"] = bundle


def append_comparison_package(
    source_path: Path, output_path: Path, sheets: list[Path], index_relationships: Path
) -> None:
    with ZipFile(source_path) as source:
        workbook_xml = source.read("xl/workbook.xml")
        existing = ET.fromstring(workbook_xml).find("s:sheets", report.NS)
        if existing is None or any(
            sheet.get("name") in SHEET_NAMES for sheet in existing
        ):
            raise ValueError(
                "source workbook has no sheets or already contains comparison sheets"
            )
        next_id = max(int(sheet.get("sheetId", "0")) for sheet in existing) + 1
        relationships = source.read("xl/_rels/workbook.xml.rels")
        used_ids = {rel.get("Id") for rel in ET.fromstring(relationships)}
        additions = {"sheets": [], "Relationships": [], "Types": []}
        entries = []
        for offset, name in enumerate(SHEET_NAMES):
            sheet_id = next_id + offset
            relationship = f"rIdCompare{sheet_id}"
            entry = f"xl/worksheets/compare{sheet_id}.xml"
            if relationship in used_ids or entry in source.namelist():
                raise ValueError("comparison worksheet package entry already exists")
            additions["sheets"].append(
                f'<sheet xmlns:r="{report.REL_NS}" name={quoteattr(name)} '
                f'sheetId="{sheet_id}" r:id="{relationship}"/>'
            )
            additions["Relationships"].append(
                f'<Relationship Id="{relationship}" '
                f'Type="{report.REL_NS}/worksheet" Target="/{entry}"/>'
            )
            additions["Types"].append(
                f'<Override PartName="/{entry}" ContentType="application/'
                'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            )
            entries.append(entry)
        replacements = {
            "xl/workbook.xml": report.append_xml(
                workbook_xml, "sheets", "".join(additions["sheets"])
            ),
            "xl/_rels/workbook.xml.rels": report.append_xml(
                relationships, "Relationships", "".join(additions["Relationships"])
            ),
            "[Content_Types].xml": report.append_xml(
                source.read("[Content_Types].xml"), "Types", "".join(additions["Types"])
            ),
        }
        with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as output:
            for info in source.infolist():
                if info.filename in replacements:
                    output.writestr(info, replacements[info.filename])
                else:
                    with source.open(info) as reader, output.open(info, "w") as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
            for entry, path in zip(entries, sheets, strict=True):
                output.write(path, entry)
            output.write(
                index_relationships,
                "xl/worksheets/_rels/" + Path(entries[-1]).name + ".rels",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--collector-root", type=Path, default=COLLECTOR)
    parser.add_argument("--baseline-run", type=Path)
    parser.add_argument("--workbook-output", type=Path)
    args = parser.parse_args()
    if (args.baseline_run is None) != (args.workbook_output is None):
        parser.error("--baseline-run and --workbook-output must be provided together")
    result = summarize(args.manifest, args.collector_root)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.workbook_output:
        comparison_workbook(result, args.baseline_run, args.workbook_output)
    (args.output_dir / "comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    save_csv(args.output_dir / "runs.csv", run_rows(result))
    save_csv(args.output_dir / "comparisons.csv", result["aggregates"])
    LOGGER.info("summarized %d accepted run-role entries", len(result["runs"]))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    raise SystemExit(main())
