#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Accept a completed Topdown run only from its saved, consistent evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import tempfile
import time
from collections.abc import Callable
from functools import partial
from itertools import pairwise
from pathlib import Path

if __package__:
    from . import compare
else:
    import compare

LOGGER = logging.getLogger(__name__)
REQUIRED_MODULES = {
    "kperf_instrument": "kperf_instrument.py",
    "vllm.v1.worker.gpu_worker": "vllm/v1/worker/gpu_worker.py",
    "vllm.v1.worker.gpu.model_runner": "vllm/v1/worker/gpu/model_runner.py",
    "vllm.v1.worker.gpu.cudagraph_utils": "vllm/v1/worker/gpu/cudagraph_utils.py",
    "vllm.v1.worker.gpu.async_utils": "vllm/v1/worker/gpu/async_utils.py",
    "vllm.v1.worker.gpu.model_states.default": (
        "vllm/v1/worker/gpu/model_states/default.py"
    ),
}
PAGE_FILES = {
    "summary.json",
    "maps",
    "stat",
    "status",
    "smaps",
    "numa_maps",
    "cgroup",
    "mountinfo",
    "mappings.csv",
    "pages.csv",
    "page_evidence.csv",
    "code_pages.csv",
    "groups_64k.csv",
    "anonymous_exec.json",
}
CHECKS = ("runtime", "placement", "pages", "cleanup", "quality", "complete", "spe")
CODE_GUARD_METHOD = "executable_vma_madv_nohugepage"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def positive(value: object, name: str) -> int:
    result = compare.integer(value, name)
    require(result > 0, f"{name} must be positive")
    return result


def digest(value: object, name: str) -> str:
    require(
        isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None,
        f"missing or invalid SHA256: {name}",
    )
    return value


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cpu_list(value: str) -> list[int]:
    require(
        isinstance(value, str)
        and re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value) is not None,
        "invalid saved CPU list",
    )
    result: set[int] = set()
    for part in value.split(","):
        ends = [int(item) for item in part.split("-")]
        low, high = ends[0], ends[-1]
        require(low <= high and high - low < 65536, "invalid CPU range")
        new = set(range(low, high + 1))
        require(not result & new, "duplicate CPU in saved list")
        result |= new
    return sorted(result)


def proc_identity(stat: str) -> tuple[int, int]:
    require(isinstance(stat, str) and ")" in stat, "missing proc stat identity")
    fields = stat.rsplit(")", 1)[1].split()
    require(len(fields) >= 20, "truncated proc stat identity")
    return positive(stat.split(" ", 1)[0], "stat PID"), positive(
        fields[19], "stat start"
    )


class Evidence:
    """Record exactly which saved files support this audit, including failures."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.proofs: dict[str, str] = {}
        self.checks = dict.fromkeys(CHECKS, "pass")
        self.reasons: list[dict[str, str]] = []
        self.details: dict[str, list[dict]] = {key: [] for key in CHECKS}

    def proof(self, path: Path) -> str:
        key = (
            str(path.relative_to(self.root))
            if path.is_relative_to(self.root)
            else str(path)
        )
        value = file_hash(path)
        if key in self.proofs:
            require(self.proofs[key] == value, f"evidence changed during audit: {key}")
        self.proofs[key] = value
        return value

    def text(self, path: Path) -> str:
        self.proof(path)
        return path.read_text(encoding="utf-8-sig")

    def json(self, path: Path) -> dict:
        value = json.loads(self.text(path))
        require(isinstance(value, dict), f"expected JSON object: {path}")
        return value

    def check(self, name: str, scope: str, action: Callable[[], dict]) -> dict | None:
        try:
            result = action()
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            csv.Error,
        ) as exc:
            self.checks[name] = "fail"
            self.reasons.append({"check": name, "scope": scope, "message": str(exc)})
            return None
        if scope == "frozen_sources":
            detail = {"files": len(result["files"]), "container": result["container"]}
        elif scope == "report_config":
            detail = {"groups": [group["name"] for group in result["groups"]]}
        else:
            detail = result
        self.details[name].append({"scope": scope, **detail})
        return result


def run_env(evidence: Evidence, folder: Path) -> dict[str, str]:
    values = {}
    for line in evidence.text(folder / "run.env").splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition("=")
        require(bool(separator) and key not in values, f"malformed run.env: {folder}")
        values[key] = value
    require(
        values.get("placement_mode") == "worker_set",
        "explicit Worker placement missing",
    )
    return values


def identity_folder(root: Path, folder: Path, env: dict) -> Path:
    session = env.get("service_session")
    if not session:
        return folder
    require(folder != root / "spe/runs/spe", "SPE must use its independent service")
    require(
        Path(session).name == "service", "unrecognized shared service identity path"
    )
    return root / "service"


def placement_identity(
    evidence: Evidence, folder: Path, env: dict
) -> tuple[dict, Path]:
    location = identity_folder(evidence.root, folder, env)
    identity = evidence.json(location / "placement_identity.json")
    for role in ("api", "worker", "engine"):
        pid = positive(identity.get(role), role)
        positive(identity["starts"].get(str(pid)), f"{role} start")
    require(
        len({identity[key] for key in ("api", "worker", "engine")}) == 3,
        "API, EngineCore and mp Worker must have distinct PIDs",
    )
    return identity, location


def source_identity(evidence: Evidence, report_config: Path) -> dict:
    frozen = evidence.json(evidence.root / "evidence/source_identity.json")
    files = frozen.get("files")
    require(isinstance(files, dict) and bool(files), "empty frozen source identity")
    for relative, record in files.items():
        require(
            not Path(relative).is_absolute() and ".." not in Path(relative).parts,
            "invalid frozen project-relative path",
        )
        require(
            isinstance(record.get("path"), str) and Path(record["path"]).is_absolute(),
            f"missing frozen source path: {relative}",
        )
        digest(record.get("sha256"), relative)
    for key in REQUIRED_MODULES.values():
        require(key in files, f"required source was not frozen: {key}")
    container = frozen.get("container", {})
    require(
        all(
            isinstance(container.get(key), str) and container[key]
            for key in ("id", "image")
        ),
        "missing frozen container identity",
    )
    require(
        evidence.proof(evidence.root / "evidence/config.env")
        == digest(frozen.get("config_sha256"), "config"),
        "actual config SHA256 mismatch",
    )
    expected = files.get("scripts/920b/report_config.json", {})
    require(
        evidence.proof(report_config)
        == digest(expected.get("sha256"), "report config"),
        "audit report configuration differs from frozen collector",
    )
    return frozen


def read_report_config(evidence: Evidence, path: Path) -> dict:
    config = evidence.json(path)
    groups = config.get("groups")
    require(
        isinstance(groups, list) and len(groups) == 13,
        "expected exactly 13 configured PMU groups plus time",
    )
    names = [group.get("name") for group in groups]
    require(
        all(
            isinstance(name, str) and re.fullmatch(r"[a-z0-9_]+", name)
            for name in names
        )
        and len(set(names)) == 13
        and "time" not in names,
        "invalid or duplicate PMU group names",
    )
    return config


def verify_complete(evidence: Evidence) -> dict:
    complete = evidence.json(evidence.root / "complete.json")
    started = positive(complete.get("started_ns"), "started_ns")
    finished = positive(complete.get("finished_ns"), "finished_ns")
    require(finished > started, "invalid complete run duration")
    require(complete.get("gpu_pids") == [], "completion GPU list is absent or nonempty")
    require(isinstance(complete.get("spe"), bool), "SPE collection state not recorded")
    require(isinstance(complete.get("report"), str), "final workbook not identified")
    workbook = evidence.root / Path(complete["report"]).name
    require(
        workbook.suffix == ".xlsx"
        and workbook.is_file()
        and workbook.stat().st_size > 0,
        "final workbook missing or empty",
    )
    evidence.proof(workbook)
    return complete


def verify_placement(
    evidence: Evidence, folder: Path, complete: dict, scope: dict
) -> dict:
    env = run_env(evidence, folder)
    identity, location = placement_identity(evidence, folder, env)
    config = identity.get("config", {})
    for key in (
        "WORKER_CPUS",
        "WORKER_POOL_CPUS",
        "WORKER_NUMA_NODE",
        "SERVICE_CPUS",
        "CLIENT_CPUS",
    ):
        require(
            config.get(key) == env.get(key.lower()),
            f"placement/run.env mismatch: {key}",
        )
    worker_cpus = cpu_list(config["WORKER_CPUS"])
    pool = cpu_list(config["WORKER_POOL_CPUS"])
    service_cpus = cpu_list(config["SERVICE_CPUS"])
    client_cpus = cpu_list(config["CLIENT_CPUS"])
    node = compare.integer(config["WORKER_NUMA_NODE"], "Worker node")
    require(
        len(worker_cpus) in (1, 2, 4)
        and len(pool) == 4
        and set(worker_cpus) <= set(pool),
        "Worker CPU count/pool violates 1/2/4-core design",
    )
    require(
        not set(pool) & (set(service_cpus) | set(client_cpus))
        and not set(service_cpus) & set(client_cpus),
        "Worker/service/client CPUs overlap",
    )
    if "cores" in scope:
        require(
            scope["cores"] == len(worker_cpus), "scope core count disagrees with Worker"
        )
    if "worker_numa_node" in scope:
        require(scope["worker_numa_node"] == node, "scope node disagrees with Worker")
    config_proof = evidence.json(folder / "placement_config.json")
    for key in ("WORKER_CPUS", "WORKER_POOL_CPUS", "SERVICE_CPUS", "CLIENT_CPUS"):
        require(
            config_proof.get(key) == cpu_list(config[key]), f"preflight mismatch: {key}"
        )
    require(config_proof.get("node") == node, "preflight node mismatch")
    records = [
        json.loads(line)
        for line in evidence.text(folder / "placement_checks.jsonl").splitlines()
        if line.strip()
    ]
    require(len(records) >= 2, "before/after placement checks missing")
    times = []
    for record in records:
        current = record.get("identity", {})
        require(current == identity, "process/placement identity changed within round")
        stamp = positive(record.get("time_ns"), "placement time")
        require(
            complete["started_ns"] <= stamp <= complete["finished_ns"],
            "placement evidence outside completed run",
        )
        times.append(stamp)
        masks = record.get("threads", {})
        require(
            masks.get(str(identity["worker"])) == worker_cpus,
            "Worker affinity not verified",
        )
        for role in ("api", "engine"):
            require(
                masks.get(str(identity[role])) == service_cpus,
                f"{role} affinity mismatch",
            )
        require(
            all(mask in (worker_cpus, service_cpus) for mask in masks.values()),
            "unexpected descendant thread affinity",
        )
        pages = record.get("pages", {})
        anonymous = pages.get("anonymous_resident_pages", {})
        require(
            isinstance(anonymous, dict) and bool(anonymous),
            "anonymous NUMA evidence missing",
        )
        require(
            positive(anonymous.get(f"N{node}"), "local anonymous pages") > 0
            and all(
                compare.integer(count, "anonymous pages") == 0
                for key, count in anonymous.items()
                if key != f"N{node}"
            ),
            "anonymous resident pages are not CPU-local",
        )
        require(
            f"bind:{node}" in pages.get("mapping_policies", []),
            "membind policy missing",
        )
    require(all(a < b for a, b in pairwise(times)), "placement times not ordered")
    if location != folder:
        rpc = evidence.json(location / "worker.json")
        require(
            rpc.get("pid") == rpc.get("tid") == identity["worker"],
            "shared RPC Worker mismatch",
        )
    return {
        "worker": identity["worker"],
        "checks": len(records),
        "cpus": worker_cpus,
        "node": node,
        "pool": pool,
        "service_cpus": service_cpus,
        "client_cpus": client_cpus,
        "identity": str(location / "placement_identity.json"),
    }


def verify_runtime(
    evidence: Evidence, folder: Path, frozen: dict, complete: dict
) -> dict:
    env = run_env(evidence, folder)
    identity, location = placement_identity(evidence, folder, env)
    worker = identity["worker"]
    runtime = evidence.json(location / "runtime_identity" / f"{worker}.json")
    require(
        runtime.get("pid") == runtime.get("tid") == worker,
        "runtime PID/TID is not placement Worker",
    )
    require(
        proc_identity(runtime.get("stat")) == (worker, identity["starts"][str(worker)]),
        "runtime process start identity mismatch",
    )
    require(
        runtime.get("cpu_affinity") == cpu_list(identity["config"]["WORKER_CPUS"]),
        "actual probed thread affinity differs from placement",
    )
    require(
        complete["started_ns"]
        <= positive(runtime.get("captured_ns"), "runtime timestamp")
        <= complete["finished_ns"],
        "runtime identity outside completed run",
    )
    modules = runtime.get("modules", {})
    require(isinstance(modules, dict), "runtime modules must be an object")
    files = frozen["files"]
    spe = folder == evidence.root / "spe/runs/spe"
    overlay = (
        evidence.json(evidence.root / "spe/evidence/overlay.json") if spe else None
    )
    verified = []
    for name, relative in REQUIRED_MODULES.items():
        actual = modules.get(name, {})
        expected = files[relative]
        expected_hash = expected["sha256"]
        if spe and name == "vllm.v1.worker.gpu.cudagraph_utils":
            require(
                overlay.get("source_sha256") == expected_hash,
                "SPE overlay source differs from frozen source",
            )
            expected_hash = digest(overlay.get("overlay_sha256"), "SPE overlay")
            require(
                str(actual.get("path", "")).endswith("/" + relative),
                "SPE overlay import path mismatch",
            )
        else:
            require(
                actual.get("path") == expected["path"],
                f"runtime import path mismatch: {name}",
            )
        require(
            actual.get("sha256") == expected_hash,
            f"runtime import SHA256 mismatch: {name}",
        )
        verified.append(name)
    by_path = {record["path"]: record for record in files.values()}
    for name, actual in modules.items():
        expected = by_path.get(actual.get("path"))
        relative = name.replace(".", "/")
        declared = files.get(relative + ".py", files.get(relative + "/__init__.py"))
        if declared and not (spe and name == "vllm.v1.worker.gpu.cudagraph_utils"):
            require(
                actual.get("path") == declared["path"],
                f"frozen module imported from different path: {name}",
            )
            expected = declared
        if expected:
            require(
                actual.get("sha256") == expected["sha256"],
                f"frozen import SHA256 mismatch: {name}",
            )
    if spe:
        marker = modules.get("spe_marker", {})
        expected = files.get("scripts/spe/marker.py", {})
        require(
            marker.get("sha256")
            == digest(expected.get("sha256"), "frozen SPE marker")
            == overlay.get("marker_sha256"),
            "SPE marker SHA256 mismatch",
        )
        overlay_path = Path(modules["vllm.v1.worker.gpu.cudagraph_utils"]["path"])
        require(
            marker.get("path") == str(overlay_path.parents[4] / "spe_marker.py"),
            "SPE marker and graph module use different overlays",
        )
        require(overlay.get("pmu") == "disabled", "SPE overlay must disable PMU")
        capture = evidence.json(evidence.root / "spe/spe_capture.json")
        require(capture.get("container_tid") == worker, "SPE container Worker mismatch")
        host_tid = positive(capture.get("host_tid"), "SPE host TID")
        require(
            capture.get("cpus") == cpu_list(identity["config"]["WORKER_CPUS"]),
            "SPE capture CPUs mismatch",
        )
        container = evidence.json(evidence.root / "spe/evidence/container.json")
        require(
            all(
                container.get(key) == frozen["container"][key]
                for key in ("id", "image")
            ),
            "SPE uses a different container identity",
        )
        for phase in ("before", "after"):
            host = evidence.root / "spe/evidence" / phase
            require(
                proc_identity(evidence.text(host / "stat"))
                == (host_tid, identity["starts"][str(worker)]),
                "SPE host/container process start identity mismatch",
            )
            status = evidence.text(host / "status")
            ids = re.search(r"^NSpid:\s+(.+)$", status, re.MULTILINE)
            require(ids is not None, "SPE host namespace PID mapping missing")
            namespace_pids = [int(value) for value in ids[1].split()]
            require(
                namespace_pids[0] == host_tid and namespace_pids[-1] == worker,
                "SPE host target does not map to the actual container Worker",
            )
            threads = json.loads(evidence.text(host / "threads.json"))
            require(
                isinstance(threads, list)
                and any(thread.get("host_tid") == host_tid for thread in threads)
                and all(thread.get("cpus") == capture["cpus"] for thread in threads),
                "SPE host thread affinity evidence missing or inconsistent",
            )
    else:
        require(
            env.get("target") == compare.STAGE
            and env.get("qualifier") == "run_fullgraph",
            "round measurement boundary differs from E2E Decode",
        )
        mode = "time" if folder.name == "time" else "pmu"
        require(env.get("mode") == mode, "round measurement mode mismatch")
        if location != folder:
            for filename, expected_mode in (
                ("switch.json", mode),
                ("stop.json", "disabled"),
            ):
                rpc = evidence.json(folder / filename)
                require(
                    rpc.get("pid") == rpc.get("tid") == worker,
                    f"{filename} Worker PID/TID mismatch",
                )
                require(
                    rpc.get("mode") == expected_mode
                    and Path(rpc.get("round_id", "")).parts[-2:]
                    == ("end_to_end", folder.name),
                    f"{filename} round/mode mismatch",
                )
        else:
            log = evidence.text(folder / "server.log")
            require(
                f"owner_pid={worker} owner_tid={worker} begin_end_owner_checks=required"
                in log,
                "old collector owner/begin/end guard evidence missing",
            )
        measurement = evidence.text(folder / "measurement.log")
        prefix = "KPERF_TIME" if mode == "time" else "KPERF"
        rows = [
            line
            for line in measurement.splitlines()
            if f"{prefix},{compare.STAGE}," in line
        ]
        require(bool(rows), "actual measurement log has no E2E rows")
        require(
            all(
                re.search(r"\(Worker(?:[^)]*) pid=" + str(worker) + r"\)", line)
                is not None
                for line in rows
            ),
            "measurement emitted outside actual Worker",
        )
    return {
        "pid": worker,
        "tid": worker,
        "required_modules": verified,
        "runtime_identity": str(location / "runtime_identity" / f"{worker}.json"),
    }


def code_guard_identity(manifest: dict) -> tuple[dict, dict] | None:
    if "code_page_guard" not in manifest:
        return None
    guard = manifest["code_page_guard"]
    require(
        isinstance(guard, dict)
        and set(guard) == {"path", "sha256", "method"}
        and guard.get("method") == CODE_GUARD_METHOD,
        "invalid code page guard descriptor/method",
    )
    path = guard.get("path")
    require(
        isinstance(path, str)
        and Path(path).is_absolute()
        and ".." not in Path(path).parts,
        "invalid code page guard path",
    )
    sha = digest(guard.get("sha256"), "code page guard")
    files = manifest.get("files")
    require(isinstance(files, list), "code page guard files manifest missing")
    matches = [
        record
        for record in files
        if isinstance(record, dict) and record.get("path") == path
    ]
    require(
        len(matches) == 1, "code page guard must have exactly one frozen copy record"
    )
    record = matches[0]
    require(
        record.get("sha256") == sha,
        "code page guard descriptor differs from frozen copy SHA256",
    )
    for key in ("copy_device", "copy_inode"):
        positive(record.get(key), f"code page guard {key}")
    return guard, record


def verify_code_guard(evidence: Evidence, manifest: dict, command: list) -> None:
    declared = code_guard_identity(manifest)
    baseline_path = evidence.root / "evidence/page_identity.json"
    baseline = (
        code_guard_identity(evidence.json(baseline_path))
        if baseline_path.is_file()
        else None
    )
    frozen = evidence.json(evidence.root / "evidence/source_identity.json")
    if declared is None and baseline is None:
        require(
            "code_page_guard" not in frozen,
            "supervisor code page guard is missing from the page identity",
        )
        return
    require(
        declared is not None and baseline is not None and declared == baseline,
        "round/SPE code page guard differs from the baseline frozen identity",
    )
    guard, _ = declared
    require(
        frozen.get("code_page_guard") == guard,
        "code page guard differs from supervisor frozen source",
    )
    values = [
        value.partition("=")[2]
        for value in command
        if isinstance(value, str) and value.startswith("CODE_PAGE_GUARD=")
    ]
    require(
        values == [guard["path"]], "launch CODE_PAGE_GUARD differs from frozen identity"
    )


def verify_pages(evidence: Evidence, folder: Path, scope: dict) -> dict:
    env = run_env(evidence, folder)
    identity, _ = placement_identity(evidence, folder, env)
    page_dir = folder / "pages"
    page_scope = "per_round"
    reference = page_dir / "reference.json"
    if reference.is_file():
        require(
            evidence.json(reference)
            == {
                "scope": "persistent_end_to_end",
                "directory": "end_to_end/pages",
            }
            and folder.parent == evidence.root / "end_to_end"
            and bool(env.get("service_session"))
            and env.get("target") == "execute_model_to_sample_tokens"
            and env.get("qualifier") == "run_fullgraph",
            "shared page evidence requires the persistent end-to-end session",
        )
        page_dir = evidence.root / "end_to_end/pages"
        page_scope = "persistent_end_to_end"
    verification = evidence.json(page_dir / "verification.json")
    observe_pages = (
        evidence.json(evidence.root / "complete.json").get("page_evidence_policy")
        == "observe"
    )
    require(
        verification.get("schema_version") == 1
        and verification.get("status")
        in (("pass", "fail") if observe_pages else ("pass",)),
        "page verification did not pass (CODE_PAGE_CONDITION evidence required)",
    )
    require(
        observe_pages
        or verification.get("reasons") == []
        and verification.get("needs_warmed_repeat") is False,
        "page verification has unresolved conditions",
    )
    mode = verification.get("mode")
    require(mode in ("4k", "64k"), "unknown actual code-folio condition")
    if "page_mode" in scope:
        require(
            mode.upper() == scope["page_mode"],
            "page mode disagrees with requested scope",
        )
    node = compare.integer(identity["config"]["WORKER_NUMA_NODE"], "Worker node")
    require(
        verification.get("expected_node") == node,
        "page audit node differs from Worker node",
    )
    identity_hash = digest(verification.get("identity_sha256"), "page identity")
    candidates = [folder / "pages/identity.json"]
    candidates.append(
        evidence.root
        / (
            "spe/evidence/page_identity.json"
            if folder == evidence.root / "spe/runs/spe"
            else "evidence/page_identity.json"
        )
    )
    saved_path = verification.get("identity")
    if isinstance(saved_path, str):
        candidates.append(Path(saved_path))
    manifest = next((path for path in candidates if path.is_file()), None)
    require(manifest is not None, "page condition identity manifest is missing")
    require(
        evidence.proof(manifest) == identity_hash,
        "page condition identity SHA256 mismatch",
    )
    namespaces = set()
    originals = set()
    launch_path = evidence.root / (
        "spe/evidence/launch.json"
        if folder == evidence.root / "spe/runs/spe"
        else "evidence/supervisor/command.json"
    )
    command = json.loads(evidence.text(launch_path))
    require(isinstance(command, list), "host launch command evidence must be a list")
    verify_code_guard(evidence, evidence.json(manifest), command)
    origins = [
        value.partition("=")[2]
        for value in command
        if isinstance(value, str) and value.startswith("CODE_PARENT_MOUNT_NAMESPACE=")
    ]
    require(
        len(origins) == 1 and re.fullmatch(r"mnt:\[\d+\]", origins[0]) is not None,
        "verified original namespace missing from host launch command",
    )
    remote_root = Path(evidence.json(evidence.root / "complete.json")["report"]).parent
    for phase in ("before", "after"):
        expected_path = page_dir / phase
        remote_expected = remote_root / expected_path.relative_to(evidence.root)
        require(
            verification.get(phase) in (str(expected_path), str(remote_expected)),
            f"page {phase} path belongs to a different run/round",
        )
        saved = verification.get("phases", {}).get(phase, {})
        require(
            saved.get("pid") == identity["worker"]
            and compare.integer(saved.get("start_ticks"), "page process start")
            == identity["starts"][str(identity["worker"])],
            "page snapshot belongs to a different Worker",
        )
        namespace = saved.get("mount_namespace")
        require(
            isinstance(namespace, str) and bool(namespace),
            "page mount namespace missing",
        )
        namespaces.add(namespace)
        original = saved.get("original_mount_namespace")
        require(
            original == origins[0] and original != namespace,
            "page original namespace disagrees with host launch or is not isolated",
        )
        originals.add(original)
        hashes = saved.get("evidence_sha256", {})
        require(
            isinstance(hashes, dict) and hashes.keys() >= PAGE_FILES,
            "page raw evidence hashes incomplete",
        )
        for name, expected in hashes.items():
            relative = Path(name)
            snapshot = (page_dir / phase).resolve()
            require(not relative.is_absolute(), "absolute raw page evidence path")
            raw_path = (snapshot / relative).resolve()
            require(
                raw_path.is_relative_to(snapshot),
                "raw page evidence path escapes snapshot",
            )
            require(
                evidence.proof(raw_path) == digest(expected, name),
                f"page evidence SHA256 mismatch: {phase}/{name}",
            )
        summary = evidence.json(page_dir / phase / "summary.json")
        require(
            summary.get("pid") == identity["worker"]
            and compare.integer(summary.get("pid_start_ticks"), "page summary start")
            == identity["starts"][str(identity["worker"])],
            "raw page summary Worker mismatch",
        )
        require(
            summary.get("identity_sha256") == identity_hash
            and summary.get("expected_node") == node,
            "raw page summary condition mismatch",
        )
        require(
            summary.get("mount_namespace") == namespace
            and summary.get("original_mount_namespace") == original,
            "raw page summary private namespace mismatch",
        )
        require(
            proc_identity(evidence.text(page_dir / phase / "stat"))
            == (identity["worker"], identity["starts"][str(identity["worker"])]),
            "raw page stat Worker mismatch",
        )
    require(len(namespaces) == 1, "mount namespace changed between page snapshots")
    require(len(originals) == 1, "original namespace changed between page snapshots")
    stability = verification.get("stability", {})
    closures = [
        verification["phases"][phase].get("runtime_closure_exceptions", [])
        for phase in ("before", "after")
    ]
    require(
        closures[0] == closures[1]
        and isinstance(closures[0], list)
        and len(closures[0]) <= 1,
        "runtime closure identity differs between endpoints",
    )
    require(
        stability.get("runtime_closure_changed") is not True,
        "runtime closure stability status contradicts endpoint changes",
    )
    if closures[0]:
        require(
            stability.get("runtime_closure_changed") is False,
            "runtime closure stability was not verified",
        )
        frozen_files = {
            record["path"]: record for record in evidence.json(manifest)["files"]
        }
        for phase in ("before", "after"):
            saved = verification["phases"][phase]
            closure = saved["runtime_closure_exceptions"][0]
            name = closure.get("dump_file")
            require(
                isinstance(name, str) and name in saved["evidence_sha256"],
                "runtime closure dump is not in the verified raw evidence",
            )
            require(
                saved["evidence_sha256"][name] == closure.get("content_sha256")
                and (page_dir / phase / name).stat().st_size == 4096
                and closure.get("page_bytes") == 4096
                and closure.get("node") == node,
                "runtime closure bytes/size/node differ from the declaration",
            )
            targets = closure.get("target_libraries")
            require(
                isinstance(targets, list) and bool(targets),
                "runtime closure targets missing",
            )
            for target in targets:
                original = frozen_files.get(target.get("path"), {})
                require(
                    all(
                        target.get(key) == original.get(key) and key in original
                        for key in ("sha256", "copy_device", "copy_inode")
                    ),
                    "runtime closure target differs from frozen page identity",
                )
    positive(stability.get("common_code_pages"), "common observed code pages")
    residency_policy = verification.get("residency_policy", "strict")
    require(residency_policy in ("strict", "observe"), "unknown page residency policy")
    require(
        residency_policy == "observe"
        or all(
            stability.get(key) == 0
            for key in (
                "newly_resident_code_pages",
                "no_longer_resident_code_pages",
                "changed_code_pages",
            )
        ),
        "page stability status contradicts code-page changes",
    )
    return {
        "mode": mode,
        "node": node,
        "scope": page_scope,
        "residency_policy": residency_policy,
        "coverage_policy": verification.get("coverage_policy", "strict"),
        "observations": verification.get("observations", []),
        "page_verdict": verification.get("status"),
        "page_findings": verification.get("reasons", []),
        "identity_sha256": identity_hash,
        "verification": str(page_dir / "verification.json"),
    }


def verify_cleanup(evidence: Evidence) -> dict:
    cleanup = evidence.json(evidence.root / "evidence/cleanup.json")
    require(cleanup.get("status") == "pass", "measured cleanup did not pass")
    for field in ("gpu_pids", "owned_processes_remaining", "perf_readers_remaining"):
        require(cleanup.get(field) == [], f"cleanup {field} is absent or nonempty")
    return cleanup


def verify_quality(evidence: Evidence, groups: set[str]) -> dict:
    evidence.proof(evidence.root / "collection_quality.csv")
    compare.verify_quality(evidence.root, groups)
    rows = compare.read_csv(evidence.root / "collection_quality.csv")
    for row in rows:
        if not row.get("group", "").startswith("end_to_end/"):
            continue
        folder = evidence.root / row["group"]
        for kind, key in (("raw", "raw"), ("parsed", "selected")):
            path = folder / kind / f"{compare.STAGE}.csv"
            evidence.proof(path)
            values = compare.read_csv(path)
            require(
                len(values) == compare.integer(row[key], key),
                f"{row['group']}: {kind} row count differs from quality",
            )
            if kind == "parsed":
                require(
                    all(value.get("valid") == "1" for value in values),
                    f"{row['group']}: invalid {kind} rows",
                )
    return {"rounds": len(groups) + 1, "stage": compare.STAGE}


def verify_spe_binary_identity(evidence: Evidence, metadata: dict) -> dict:
    manifest = evidence.root / "spe/evidence/page_identity.json"
    if (
        not manifest.exists()
        and not (evidence.root / "evidence/page_identity.json").exists()
    ):
        return {"status": "not_required", "reason": "no controlled page identity"}
    frozen = evidence.json(manifest).get("files")
    require(isinstance(frozen, list) and bool(frozen), "empty SPE page identity")
    expected = {}
    for record in frozen:
        require(isinstance(record, dict), "malformed SPE page identity record")
        path = record.get("path")
        require(
            isinstance(path, str) and Path(path).is_absolute() and path not in expected,
            "invalid or duplicate SPE page identity path",
        )
        expected[path] = digest(record.get("sha256"), f"SPE frozen binary {path}")
    actual = json.loads(evidence.text(evidence.root / "spe/evidence/binaries.json"))
    saved = metadata.get("binaries")
    indexed = []
    for label, rows in (("capture evidence", actual), ("metadata", saved)):
        require(
            isinstance(rows, list) and bool(rows),
            f"SPE {label} binaries must be a nonempty JSON list",
        )
        records = {}
        for record in rows:
            require(isinstance(record, dict), f"malformed SPE {label} binary record")
            path = record.get("mapped_path")
            require(
                isinstance(path, str)
                and Path(path).is_absolute()
                and path not in records,
                f"invalid or duplicate SPE {label} binary mapped_path",
            )
            require(
                "unavailable" not in record
                and isinstance(record.get("path"), str)
                and Path(record["path"]).is_absolute(),
                f"SPE executable mapping has no captured binary: {path}",
            )
            digest(record.get("sha256"), f"SPE {label} binary {path}")
            records[path] = record
        indexed.append(records)
    require(
        indexed[0] == indexed[1],
        "SPE metadata binaries differ from saved capture evidence",
    )
    for path, record in indexed[0].items():
        require(
            path in expected,
            f"SPE executable mapping missing from page identity: {path}",
        )
        require(
            record["sha256"] == expected[path],
            f"SPE captured binary SHA256 differs from frozen page identity: {path}",
        )
    return {"status": "pass", "files": len(indexed[0])}


def verify_spe(evidence: Evidence) -> dict:
    audit = evidence.json(evidence.root / "spe/analysis/audit.json")
    manifest = evidence.json(evidence.root / "spe/analysis/manifest.json")
    capture = evidence.json(evidence.root / "spe/capture_complete.json")
    require(
        audit.get("status") == manifest.get("status") == "pass",
        "SPE normalization did not pass",
    )
    for field in ("selected_samples", "pc_count"):
        require(
            positive(audit.get(field), f"SPE {field}") == manifest.get(field),
            "SPE audit/manifest counts disagree",
        )
    require(
        capture.get("owned_worker_released") is True
        and capture.get("remaining_gpu_pids") == []
        and capture.get("perf_readers_complete") is True,
        "SPE cleanup is incomplete",
    )
    compare.verify_spe_independent_checks(audit, evidence.root / "spe")
    if (evidence.root / "spe/analysis/retention.json").exists():
        evidence.json(evidence.root / "spe/analysis/retention.json")
    metadata = evidence.json(evidence.root / "spe/spe_capture.json")
    scope = manifest.get("scope", {})
    for field in ("host_tid", "container_tid", "cpus"):
        require(
            scope.get(field) == metadata.get(field),
            f"SPE decoded {field} differs from capture",
        )
    require(
        scope.get("window_kind") == "run_fullgraph"
        and scope.get("formal_requests_only") is True,
        "SPE selected scope is not formal run_fullgraph",
    )
    return {
        "selected_samples": audit["selected_samples"],
        "pc_count": audit["pc_count"],
        "binaries": verify_spe_binary_identity(evidence, metadata),
    }


def audit_run(root: Path, report_config: Path, scope: dict | None = None) -> dict:
    """Return fail with independent reasons; never promote missing evidence to pass."""
    root = root.resolve()
    scope = dict(scope or {})
    evidence = Evidence(root)
    complete = evidence.check("complete", "run", lambda: verify_complete(evidence))
    frozen = evidence.check(
        "runtime", "frozen_sources", lambda: source_identity(evidence, report_config)
    )
    config = evidence.check(
        "quality", "report_config", lambda: read_report_config(evidence, report_config)
    )
    groups = {entry["name"] for entry in config.get("groups", [])} if config else set()
    evidence.check("quality", "all_rounds", lambda: verify_quality(evidence, groups))
    evidence.check("cleanup", "run", lambda: verify_cleanup(evidence))
    rounds = [root / "end_to_end" / group for group in sorted(groups | {"time"})]
    if complete and complete["spe"]:
        rounds.append(root / "spe/runs/spe")
        evidence.check("spe", "independent_audits", lambda: verify_spe(evidence))
    elif complete:
        evidence.checks["spe"] = "not_collected"
    else:
        evidence.checks["spe"] = "not_verified"
    for folder in rounds:
        label = str(folder.relative_to(root))
        evidence.check(
            "placement",
            label,
            partial(verify_placement, evidence, folder, complete or {}, scope),
        )
        evidence.check(
            "runtime",
            label,
            partial(verify_runtime, evidence, folder, frozen or {}, complete or {}),
        )
        evidence.check("pages", label, partial(verify_pages, evidence, folder, scope))
    modes = {item["mode"] for item in evidence.details["pages"]}
    if len(modes) > 1:
        evidence.check(
            "pages",
            "run",
            lambda: require(False, "code-folio mode changed across rounds"),
        )
    placements = {
        (
            tuple(item["cpus"]),
            item["node"],
            tuple(item["pool"]),
            tuple(item["service_cpus"]),
            tuple(item["client_cpus"]),
        )
        for item in evidence.details["placement"]
    }
    if len(placements) > 1:
        evidence.check(
            "placement",
            "run",
            lambda: require(False, "placement changed across rounds"),
        )
    observe_pages = bool(complete and complete.get("page_evidence_policy") == "observe")
    if observe_pages and evidence.checks["pages"] == "pass":
        evidence.checks["pages"] = "observed"
    result = {
        "schema_version": 1,
        "status": "fail" if evidence.reasons else "pass",
        "run_dir": str(root),
        "verified_at_ns": time.time_ns(),
        "checks": evidence.checks,
        "scope": scope,
        "reasons": evidence.reasons,
        "details": evidence.details,
        "proofs": evidence.proofs,
        "limitations": [
            (
                "Page stability is verified at the saved endpoints, "
                "not continuously inside measurement windows."
            ),
            (
                "Scope labels are caller-supplied; measured core count, "
                "code-folio mode and supplied node are cross-checked."
            ),
            (
                "Time and PMU rounds remain independent; this audit makes no "
                "cycles/time or SPE execution-count inference."
            ),
        ],
    }
    if result["status"] == "pass" and not observe_pages:
        # Reuse the comparison's final gate without writing a provisional acceptance.
        with tempfile.TemporaryDirectory(prefix="topdown_acceptance_") as temporary:
            candidate = Path(temporary) / "candidate.json"
            candidate.write_text(json.dumps(result), encoding="utf-8")
            entry = {
                "run_dir": str(root),
                "audit_file": str(candidate),
                **{key: scope[key] for key in compare.FACTORS if key in scope},
            }
            evidence.check(
                "complete",
                "comparison_gate",
                lambda: {"report": str(compare.verify_acceptance(entry)[2])},
            )
        result["status"] = "fail" if evidence.reasons else "pass"
    return result


def default_report_config() -> Path:
    installed = Path(__file__).parent / "920b/report_config.json"
    return (
        installed
        if installed.is_file()
        else compare.COLLECTOR / "scripts/920b/report_config.json"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--scope", type=Path)
    parser.add_argument("--report-config", type=Path, default=default_report_config())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    scope = compare.read_json(args.scope) if args.scope else {}
    result = audit_run(args.run_dir, args.report_config, scope)
    output = args.output or args.run_dir / "acceptance.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    LOGGER.info(
        "Run acceptance %s: %s (%d reasons)",
        result["status"],
        output,
        len(result["reasons"]),
    )
    raise SystemExit(0 if result["status"] == "pass" else 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
