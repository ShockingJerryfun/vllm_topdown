#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Run the ordered, resumable Topdown comparison using already prepared pages.

Settings use projects.{new,old}; localities.{near,far}, each with node, pool,
service, client, and core_sets.{1,2,4}; page_conditions.{near_4k,near_64k,far_64k};
base_env; results_dir; config_dir; output_dir; and repeats (default 3).
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if __package__:
    from . import audit_run, compare
else:
    import audit_run
    import compare

LOGGER = logging.getLogger(__name__)
CORE_ORDERS = ((1, 2, 4), (4, 1, 2), (2, 4, 1))
COMPARISON_CONDITIONS = (
    ("new", "64K", "near", "baseline"),
    ("new", "4K", "near", "pages"),
    ("old", "64K", "near", "scripts"),
    ("new", "64K", "far", "locality"),
)
CONTROLLED_ENV = {
    "PROJECT",
    "RUN_ROOT",
    "TOPDOWN_CONFIG",
    "COLLECTION_PROFILE",
    "SPE_ENABLE",
    "PLACEMENT_MODE",
    "WORKER_CPUS",
    "WORKER_POOL_CPUS",
    "WORKER_NUMA_NODE",
    "SERVICE_CPUS",
    "CLIENT_CPUS",
    "CODE_PAGE_CONDITION",
    "CODE_PAGE_MODE",
    "CODE_PAGE_GUARD",
}
REQUIRED_ENV = {
    "HOST_PYTHON",
    "CONTAINER",
    "PYTHON_BIN",
    "CHIP",
    "EXPERIMENT_LOCK",
    "SPE_BINARY_CACHE",
    "MODEL",
    "RANDOM_INPUT_LEN",
    "RANDOM_OUTPUT_LEN",
    "SERVER_FLAGS",
    "SUBREAPER_BIN",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def save_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def signature(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_path(value: object, parent: Path) -> str:
    require(
        isinstance(value, str) and bool(value), "expected a nonempty filesystem path"
    )
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else parent / path).resolve())


def cpus(value: object, label: str) -> list[int]:
    require(
        isinstance(value, list)
        and bool(value)
        and all(
            isinstance(cpu, int) and not isinstance(cpu, bool) and cpu >= 0
            for cpu in value
        ),
        f"{label} must be a nonempty integer CPU list",
    )
    require(len(set(value)) == len(value), f"{label} contains duplicate CPUs")
    return sorted(value)


def settings_file(path: Path) -> dict:
    settings = compare.read_json(path)
    require(isinstance(settings.get("base_env"), dict), "base_env must be an object")
    base = {}
    for key, value in settings["base_env"].items():
        require(
            re.fullmatch(r"[A-Z][A-Z0-9_]*", key) is not None,
            "invalid configuration variable name",
        )
        require(
            key not in CONTROLLED_ENV, f"{key} is controlled by the sweep, not base_env"
        )
        require(
            isinstance(value, (str, int, float, bool)), f"base_env.{key} must be scalar"
        )
        text = str(int(value)) if isinstance(value, bool) else str(value)
        require(
            not any(character in text for character in "\x00\r\n"),
            f"base_env.{key} contains a line break",
        )
        base[key] = text
    require(
        base.keys() >= REQUIRED_ENV,
        "base_env missing: " + ", ".join(sorted(REQUIRED_ENV - base.keys())),
    )
    require(base["CHIP"] == "920b", "this 14-round comparison requires CHIP=920b")
    require(
        "--enforce-eager" not in shlex.split(base["SERVER_FLAGS"]),
        "run_fullgraph comparison requires graph mode",
    )
    for key in ("RANDOM_INPUT_LEN", "RANDOM_OUTPUT_LEN"):
        audit_run.positive(base[key], key)
    require(
        int(base["RANDOM_OUTPUT_LEN"]) >= 3, "at least two Decode calls are required"
    )
    for key, value in {
        "NUM_PROMPTS": "1",
        "NUM_WARMUPS": "0",
        "READY_CHECK_TIMEOUT_SEC": "0",
        "TENSOR_PARALLEL_SIZE": "1",
        "DATA_PARALLEL_SIZE": "1",
    }.items():
        require(base.get(key, value) == value, f"{key} must be {value}")
        base[key] = value
    settings["base_env"] = base
    repeats = settings.get("repeats", 3)
    require(
        isinstance(repeats, int) and not isinstance(repeats, bool) and repeats >= 2,
        "repeats must be at least two; default three",
    )
    settings["repeats"] = repeats
    for key in ("results_dir", "config_dir", "output_dir"):
        settings[key] = resolve_path(settings.get(key), path.parent)
    require(
        len({settings[key] for key in ("results_dir", "config_dir", "output_dir")})
        == 3,
        "results, config and output directories must be distinct",
    )
    projects = settings.get("projects", {})
    require(
        isinstance(projects, dict) and set(projects) == {"old", "new"},
        "projects must define old and new",
    )
    for script in ("old", "new"):
        projects[script] = resolve_path(projects[script], path.parent)
        root = Path(projects[script])
        for name in ("run_experiment.sh", "build_xlsx.py", "920b/report_config.json"):
            require(
                (root / "scripts" / name).is_file(),
                f"collector entrypoint missing: {root / 'scripts' / name}",
            )
    require(
        projects["old"] != projects["new"],
        "old and new must identify separate collectors",
    )
    hashes = {
        audit_run.file_hash(Path(projects[script]) / "scripts/920b/report_config.json")
        for script in projects
    }
    require(len(hashes) == 1, "old/new PMU event/formula report configurations differ")
    settings["event_config_hash"] = signature(
        {
            "report_config": hashes.pop(),
            "overrides": {
                key: value
                for key, value in base.items()
                if key.startswith(("EVENTS_", "NAMES_"))
            },
        }
    )
    localities = settings.get("localities", {})
    require(
        isinstance(localities, dict) and set(localities) == {"near", "far"},
        "define near and far locality",
    )
    for label, locality in localities.items():
        require(isinstance(locality, dict), f"{label} locality must be an object")
        node = locality.get("node")
        require(
            isinstance(node, int) and not isinstance(node, bool) and 0 <= node < 4,
            "locality node must be 0..3",
        )
        for key in ("pool", "service", "client"):
            locality[key] = cpus(locality.get(key), f"{label}.{key}")
        require(len(locality["pool"]) == 4, f"{label} must have a fixed four-core pool")
        pool, service, client = (
            set(locality[key]) for key in ("pool", "service", "client")
        )
        require(
            not pool & (service | client) and not service & client,
            f"{label} CPU sets overlap",
        )
        sets = locality.get("core_sets", {})
        require(
            isinstance(sets, dict) and set(sets) == {"1", "2", "4"},
            f"{label} core_sets must define 1,2,4",
        )
        for count in (1, 2, 4):
            sets[str(count)] = cpus(sets[str(count)], f"{label}.core_sets.{count}")
            require(
                len(sets[str(count)]) == count and set(sets[str(count)]) <= pool,
                f"{label} {count}-core set is not a matching subset of its pool",
            )
    require(
        localities["near"]["node"] != localities["far"]["node"],
        "near and far NUMA nodes must differ",
    )
    conditions = settings.get("page_conditions", {})
    require(
        isinstance(conditions, dict)
        and set(conditions) == {"near_4k", "near_64k", "far_64k"},
        "define exactly near_4k, near_64k and far_64k page conditions",
    )
    for name, condition in list(conditions.items()):
        record = {"path": condition} if isinstance(condition, str) else dict(condition)
        record["path"] = resolve_path(record.get("path"), path.parent)
        record["prefault"] = resolve_path(
            record.get("prefault", "prefault.tsv"), Path(record["path"])
        )
        require(
            record["prefault"] == str(Path(record["path"]) / "prefault.tsv"),
            "the existing runner requires condition/prefault.tsv",
        )
        conditions[name] = record
    require(
        len({value["path"] for value in conditions.values()}) == 3,
        "page conditions must use three distinct prepared copy directories",
    )
    return settings


def verify_condition(settings: dict, name: str) -> dict:
    """Inspect saved prefault proof and inode metadata without reading copy bytes."""
    locality, mode = name.split("_")
    placement = settings["localities"][locality]
    condition = settings["page_conditions"][name]
    root = Path(condition["path"])
    require(
        not (root / "files").is_symlink(),
        "prepared copy directory must not be a symlink",
    )
    for required in (
        "identity.json",
        "paths.txt",
        "segments.tsv",
        "files/.task_owned_copies",
    ):
        require(
            (root / required).is_file(),
            f"page condition is not prepared: {root / required}",
        )
    manifest = compare.read_json(root / "identity.json")
    guard = audit_run.code_guard_identity(manifest)
    require(guard is not None, "formal page condition requires a code page guard")
    records = manifest.get("files")
    require(isinstance(records, list) and bool(records), f"empty page identity: {root}")
    paths = [record["path"] for record in records]
    require(
        len(set(paths)) == len(paths)
        and set(paths) == set((root / "paths.txt").read_text().splitlines()),
        f"page identity/path manifest mismatch: {root}",
    )
    for record in records:
        original = Path(record["path"])
        require(
            original.is_absolute() and ".." not in original.parts,
            "invalid prepared source path",
        )
        copied = root / "files" / original.relative_to("/")
        require(
            copied.resolve().is_relative_to((root / "files").resolve()),
            f"prepared copy path escapes condition: {copied}",
        )
        require(
            copied.is_file() and not copied.is_symlink(),
            f"prepared independent copy missing: {copied}",
        )
        stat = copied.stat()
        require(
            stat.st_nlink == 1
            and (stat.st_dev, stat.st_ino)
            == (record.get("copy_device"), record.get("copy_inode")),
            f"prepared copy inode changed: {copied}",
        )
        require(
            all(
                isinstance(record.get(key), int) and record[key] > 0
                for key in ("source_device", "source_inode")
            )
            and (record.get("source_device"), record.get("source_inode"))
            != (stat.st_dev, stat.st_ino)
            and stat.st_size == record.get("size"),
            f"copy is not independent or size changed: {copied}",
        )
        audit_run.digest(record.get("sha256"), str(original))
    segments = []
    for line in (root / "segments.tsv").read_text().splitlines():
        values = line.split("\t")
        require(
            len(values) == 3 and values[0] in paths, f"invalid prepared segment: {root}"
        )
        segments.append(
            (
                values[0],
                compare.integer(values[1], "offset"),
                audit_run.positive(values[2], "segment size"),
            )
        )
    require(bool(segments), f"no prepared executable segments: {root}")
    declared = []
    for record in records:
        ranges = record.get("segments")
        if record.get("data_only") is True:
            require(ranges == [], "data-only copy must not have executable segments")
            continue
        require(
            isinstance(ranges, list) and bool(ranges),
            "frozen ELF executable segments missing",
        )
        for segment in ranges:
            offset = compare.integer(segment.get("offset"), "frozen segment offset")
            size = audit_run.positive(segment.get("size"), "frozen segment size")
            require(
                0 <= offset < offset + size <= record["size"],
                "frozen segment exceeds copy size",
            )
            declared.append((record["path"], offset, size))
    require(
        Counter(declared) == Counter(segments),
        "segment manifest differs from frozen ELF ranges",
    )
    prefault = Path(condition["prefault"])
    with prefault.open(newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    observed = []
    for row in rows:
        require(
            row.get("mode") == mode
            and compare.integer(row.get("node"), "prefault node") == placement["node"]
            and compare.integer(row.get("cpu"), "prefault CPU") in placement["pool"],
            f"prefault mode/CPU/node differs from {name}",
        )
        audit_run.positive(row.get("fault_touches"), "prefault touches")
        observed.append(
            (
                row["path"],
                compare.integer(row.get("offset"), "prefault offset"),
                audit_run.positive(row.get("size"), "prefault size"),
            )
        )
    require(
        Counter(observed) == Counter(segments),
        f"prefault did not finish every executable segment: {name}",
    )
    return {
        "identity_sha256": audit_run.file_hash(root / "identity.json"),
        "prefault_sha256": audit_run.file_hash(prefault),
        "segments": len(segments),
        "code_page_guard": guard[0],
    }


def specification(
    phase: str,
    repeat: int,
    script: str,
    cores: int,
    page: str,
    locality: str,
    role: str,
) -> dict:
    return {
        "id": f"{phase}_r{repeat:02d}_{script}_c{cores}_{page.lower()}_{locality}",
        "phase": phase,
        "repeat": repeat,
        "script": script,
        "cores": cores,
        "page_mode": page,
        "locality": locality,
        "role": role,
    }


def phase_plan(settings: dict, phase: str, selected: int | None = None) -> list[dict]:
    result = []
    for index in range(settings["repeats"]):
        if phase == "cores":
            result.extend(
                specification("cores", index + 1, "new", cores, "64K", "near", "cores")
                for cores in CORE_ORDERS[index % len(CORE_ORDERS)]
            )
        else:
            require(
                selected in (1, 2, 4),
                "comparisons require a completed, accepted core selection",
            )
            shift = index % len(COMPARISON_CONDITIONS)
            order = COMPARISON_CONDITIONS[shift:] + COMPARISON_CONDITIONS[:shift]
            result.extend(
                specification(
                    "comparisons", index + 1, script, selected, page, locality, role
                )
                for script, page, locality, role in order
            )
    return result


def run_scope(settings: dict, run: dict) -> dict:
    return {
        **{key: run[key] for key in compare.FACTORS},
        "worker_numa_node": settings["localities"][run["locality"]]["node"],
        "model": settings["base_env"]["MODEL"],
        "input_len": int(settings["base_env"]["RANDOM_INPUT_LEN"]),
        "output_len": int(settings["base_env"]["RANDOM_OUTPUT_LEN"]),
        "execution_mode": "graph",
        "event_config_hash": settings["event_config_hash"],
        "window": compare.STAGE + ":Decode:run_fullgraph",
    }


def config_text(settings: dict, run: dict) -> str:
    locality = settings["localities"][run["locality"]]
    condition = settings["page_conditions"][
        run["locality"] + "_" + run["page_mode"].lower()
    ]
    values = {
        **settings["base_env"],
        "PROJECT": settings["projects"][run["script"]],
        "COLLECTION_PROFILE": "end_to_end",
        "SPE_ENABLE": "1" if run.get("spe", True) else "0",
        "PLACEMENT_MODE": "worker_set",
        "WORKER_CPUS": ",".join(map(str, locality["core_sets"][str(run["cores"])])),
        "WORKER_POOL_CPUS": ",".join(map(str, locality["pool"])),
        "WORKER_NUMA_NODE": str(locality["node"]),
        "SERVICE_CPUS": ",".join(map(str, locality["service"])),
        "CLIENT_CPUS": ",".join(map(str, locality["client"])),
        "CODE_PAGE_CONDITION": condition["path"],
        "CODE_PAGE_MODE": run["page_mode"].lower(),
    }
    return "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in sorted(values.items())
    )


def write_configs(settings: dict, runs: list[dict]) -> None:
    folder = Path(settings["config_dir"])
    folder.mkdir(parents=True, exist_ok=True)
    for run in runs:
        path = folder / (run["id"] + ".env")
        value = config_text(settings, run)
        if path.exists():
            require(
                path.read_text() == value, f"existing config differs; preserved: {path}"
            )
        else:
            with path.open("x", encoding="utf-8") as stream:
                stream.write(value)
            path.chmod(0o600)


@contextmanager
def sweep_lock(folder: Path) -> Iterator[None]:
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "lock").open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another sweep is active; no run was started") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


class Sweep:
    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.folder = Path(settings["results_dir"]) / ".sweep"
        self.state_path = self.folder / "state.json"
        self.state = (
            compare.read_json(self.state_path)
            if self.state_path.exists()
            else {
                "schema_version": 1,
                "settings_sha256": signature(settings),
                "status": "prepared",
                "phases": {"cores": "pending", "comparisons": "deferred"},
                "selected_cores": None,
                "current_run": None,
                "runs": {},
                "source_hashes": {},
            }
        )
        require(
            self.state.get("settings_sha256") == signature(settings),
            "settings changed for an existing sweep; "
            "preserve its runs and use separate directories",
        )
        self.accepted: list[dict] = []

    def save(self) -> None:
        self.state["updated_ns"] = time.time_ns()
        save_json(self.state_path, self.state)

    def prepare(self, phase: str) -> list[dict]:
        cores = phase_plan(self.settings, "cores")
        selected = self.state["selected_cores"]
        comparisons = (
            phase_plan(self.settings, "comparisons", selected) if selected else []
        )
        write_configs(self.settings, cores + comparisons)
        save_json(
            self.folder / "plan.json",
            {
                "schema_version": 1,
                "settings": self.settings,
                "core_runs": cores,
                "comparison_runs": comparisons,
                "comparison_status": "planned"
                if comparisons
                else "deferred_until_accepted_core_selection",
                "comparison_conditions": COMPARISON_CONDITIONS,
                "expected_physical_runs": self.settings["repeats"] * 7,
                "selected_cores": selected,
                "requested_phase": phase,
            },
        )
        self.save()
        return cores if phase == "cores" else comparisons

    def prerequisites(self, phase: str) -> None:
        names = (
            ["near_64k"] if phase == "cores" else list(self.settings["page_conditions"])
        )
        evidence = {name: verify_condition(self.settings, name) for name in names}
        previous = self.state.setdefault("page_proofs", {})
        require(
            len(
                {
                    signature(proof["code_page_guard"])
                    for proof in (*previous.values(), *evidence.values())
                }
            )
            == 1,
            "code page guard differs across prepared conditions",
        )
        for name, proof in evidence.items():
            require(
                name not in previous or previous[name] == proof,
                f"prepared condition changed during sweep: {name}",
            )
        previous.update(evidence)
        self.save()

    def entries(self, run: dict, audit_path: Path) -> list[dict]:
        roles = (
            ("pages", "scripts", "locality")
            if run["role"] == "baseline"
            else (run["role"],)
        )
        return [
            {
                "run_dir": str(Path(self.settings["results_dir"]) / run["id"]),
                "audit_file": str(audit_path),
                **{key: run[key] for key in compare.FACTORS},
                "repeat": run["repeat"],
                "role": role,
                "baseline": run["role"] == "baseline"
                or (run["role"] == "cores" and run["cores"] == 1),
            }
            for role in roles
        ]

    def accept(self, run: dict, existing: bool) -> None:
        root = Path(self.settings["results_dir"]) / run["id"]
        scope = run_scope(self.settings, run)
        self.state["current_run"] = run["id"]
        self.state["runs"][run["id"]] = {**run, "status": "validating"}
        self.save()
        expected_spe = run.get("spe", True)
        if existing:
            complete, acceptance, _ = compare.verify_acceptance(
                {"run_dir": str(root), **scope}
            )
            require(complete["spe"] is expected_spe, f"SPE policy mismatch: {root}")
            require(
                all(
                    acceptance.get("checks", {}).get(key) == "pass"
                    for key in ("quality", "complete")
                ),
                f"existing run lacks full acceptance: {root}",
            )
        require(
            (root / "evidence/config.env").read_text()
            == config_text(self.settings, run),
            f"run used a different experiment config: {root}",
        )
        condition_name = run["locality"] + "_" + run["page_mode"].lower()
        prepared = self.state["page_proofs"][condition_name]
        for name, key in (
            ("page_identity.json", "identity_sha256"),
            ("prefault.tsv", "prefault_sha256"),
        ):
            require(
                audit_run.file_hash(root / "evidence" / name) == prepared[key],
                f"run did not use the prepared {condition_name} {name}",
            )
        if expected_spe:
            base_files = compare.read_json(root / "evidence/page_identity.json")[
                "files"
            ]
            extended = compare.read_json(root / "spe/evidence/page_identity.json")[
                "files"
            ]
            by_path = {record["path"]: record for record in extended}
            require(
                all(by_path.get(record["path"]) == record for record in base_files),
                "SPE page identity does not retain the prepared baseline files",
            )
        collector = Path(self.settings["projects"][run["script"]])
        result = audit_run.audit_run(
            root, collector / "scripts/920b/report_config.json", scope
        )
        audit_path = self.folder / "audits" / run["id"] / "acceptance.json"
        save_json(audit_path, result)
        if not existing:
            save_json(root / "acceptance.json", result)
        require(
            result["status"] == "pass"
            and result["checks"]["spe"]
            == ("pass" if expected_spe else "not_collected"),
            f"run acceptance failed; evidence preserved: {root}; see {audit_path}",
        )
        frozen = compare.read_json(root / "evidence/source_identity.json")
        require(
            frozen["files"]["kperf_instrument.py"]["path"]
            == str(collector / "kperf_instrument.py"),
            f"run source does not match selected {run['script']} collector",
        )
        code_hash = signature(
            {name: record["sha256"] for name, record in frozen["files"].items()}
        )
        known = self.state["source_hashes"].get(run["script"])
        require(
            known is None or known == code_hash,
            f"{run['script']} source changed between repeated runs",
        )
        self.state["source_hashes"][run["script"]] = code_hash
        self.accepted.extend(self.entries(run, audit_path))
        self.state["runs"][run["id"]] = {
            **run,
            "status": "accepted",
            "reused": existing,
            "acceptance": str(audit_path),
            "source_sha256": code_hash,
        }
        self.state["current_run"] = None
        self.save()

    def collect(self, run: dict) -> None:
        root = Path(self.settings["results_dir"]) / run["id"]
        if root.exists():
            LOGGER.info("Revalidate existing run %s", run["id"])
            self.accept(run, existing=True)
            return
        require(
            self.state["runs"].get(run["id"], {}).get("status") != "blocked",
            f"previous attempt failed; not retrying or replacing {run['id']}",
        )
        self.prerequisites(run["phase"])
        self.state["status"] = "running"
        self.state["current_run"] = run["id"]
        self.state["runs"][run["id"]] = {**run, "status": "running"}
        self.save()
        config = Path(self.settings["config_dir"]) / (run["id"] + ".env")
        command = [
            "bash",
            str(
                Path(self.settings["projects"][run["script"]])
                / "scripts/run_experiment.sh"
            ),
            str(config),
            str(root),
        ]
        log = self.folder / "logs" / (run["id"] + ".log")
        log.parent.mkdir(parents=True, exist_ok=True)
        LOGGER.info(
            "Collect %s (time + 13 PMU%s); log: %s",
            run["id"],
            " + SPE" if run.get("spe", True) else "",
            log,
        )
        with log.open("xb") as stream:
            subprocess.run(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=True,
                env={
                    **os.environ,
                    "COLLECTION_PROFILE": "end_to_end",
                    "SPE_ENABLE": "1" if run.get("spe", True) else "0",
                },
            )
        self.accept(run, existing=False)

    def summarize(self, final: bool = False) -> dict:
        require(bool(self.accepted), "no accepted inputs are available")
        manifest = self.folder / "manifest.json"
        save_json(manifest, self.accepted)
        result = compare.summarize(manifest, Path(self.settings["projects"]["new"]))
        result["sweep"] = {
            "status": "all_complete" if final else "partial",
            "accepted_physical_runs": len(
                {entry["run_dir"] for entry in self.accepted}
            ),
            "expected_physical_runs": self.settings["repeats"] * 7,
            "phases": self.state["phases"],
            "selected_cores": self.state["selected_cores"],
        }
        folder = self.folder / "summary"
        folder.mkdir(parents=True, exist_ok=True)
        if final:
            destination = Path(self.settings["output_dir"])
            destination.mkdir(parents=True, exist_ok=True)
            baseline = next(
                Path(entry["run_dir"])
                for entry in self.accepted
                if entry["role"] == "pages"
                and entry["baseline"]
                and entry["repeat"] == 1
            )
            compare.comparison_workbook(
                result, baseline, destination / "Topdown_对比.xlsx"
            )
            save_json(destination / "comparison.json", result)
            compare.save_csv(destination / "runs.csv", compare.run_rows(result))
            compare.save_csv(destination / "comparisons.csv", result["aggregates"])
        save_json(folder / "comparison.json", result)
        compare.save_csv(folder / "runs.csv", compare.run_rows(result))
        compare.save_csv(folder / "comparisons.csv", result["aggregates"])
        return result

    def core_selection(self, collect: bool) -> None:
        runs = self.prepare("cores")
        self.state["phases"]["cores"] = "running" if collect else "validating"
        self.save()
        for run in runs:
            if collect:
                self.collect(run)
                self.summarize()
            else:
                self.accept(run, existing=True)
        result = self.summarize()
        selected = result["core_selection"]["suggested_cores"]
        require(
            result["core_selection"]["coverage"] == "all_1_2_4"
            and selected in (1, 2, 4),
            "complete accepted core repetitions did not yield a core suggestion",
        )
        require(
            self.state["selected_cores"] in (None, selected),
            "core selection changed on resume; "
            "existing comparison conditions preserved",
        )
        self.state["selected_cores"] = selected
        self.state["phases"]["cores"] = "complete"
        self.state["status"] = (
            "all_complete"
            if self.state["phases"]["comparisons"] == "complete"
            else "partial"
        )
        save_json(
            self.folder / "core_selection.json",
            {
                "selected_cores": selected,
                "selection": result["core_selection"],
                "accepted_runs": [
                    entry for entry in self.accepted if entry["role"] == "cores"
                ],
            },
        )
        self.save()
        self.summarize()
        LOGGER.info(
            "Accepted core selection: %d; comparisons remain separate", selected
        )

    def execute(self, phase: str, prepare_only: bool) -> dict:
        try:
            self.state.pop("error", None)
            self.prepare(phase)
            self.prerequisites(phase)
            if prepare_only:
                if phase == "comparisons":
                    self.core_selection(collect=False)
                    self.prepare(phase)
                self.prerequisites(phase)
                self.state["last_action"] = "prepare_only"
                self.save()
                return self.state
            if phase in ("cores", "all"):
                self.core_selection(collect=True)
            else:
                self.core_selection(collect=False)
            if phase != "cores":
                self.prerequisites("comparisons")
                runs = self.prepare("comparisons")
                self.state["phases"]["comparisons"] = "running"
                self.save()
                for run in runs:
                    self.collect(run)
                    self.summarize()
                self.state["phases"]["comparisons"] = "complete"
                self.summarize(final=True)
                self.state["status"] = "all_complete"
                self.state["current_run"] = None
                self.save()
            return self.state
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            subprocess.CalledProcessError,
            KeyboardInterrupt,
        ) as exc:
            current = self.state.get("current_run")
            if current:
                self.state["runs"][current]["status"] = "blocked"
                self.state["runs"][current]["error"] = str(exc) or "interrupted"
            self.state["status"] = "blocked"
            self.state["error"] = str(exc) or "interrupted"
            self.save()
            raise


def run(settings_path: Path, phase: str, prepare_only: bool = False) -> dict:
    require(phase in ("cores", "comparisons", "all"), "unknown sweep phase")
    settings = settings_file(settings_path.resolve())
    with sweep_lock(Path(settings["results_dir"]) / ".sweep"):
        return Sweep(settings).execute(phase, prepare_only)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument(
        "--phase", choices=("cores", "comparisons", "all"), required=True
    )
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    try:
        state = run(args.settings, args.phase, args.prepare_only)
    except KeyboardInterrupt:
        LOGGER.error("Sweep interrupted; saved runs are preserved")
        raise SystemExit(130) from None
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as exc:
        LOGGER.error("Sweep stopped: %s", exc)
        raise SystemExit(1) from exc
    LOGGER.info(
        "Sweep %s; phases=%s selected_cores=%s",
        state["status"],
        state["phases"],
        state["selected_cores"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
