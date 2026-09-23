# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Run a complete owned Topdown/SPE experiment with thermal and cleanup checks."""

import argparse
import fcntl
import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

if __package__:
    from .capture import (
        code_page_guard,
        gpu_pids,
        gpu_state,
        idle_container,
        output,
        save,
    )
    from .compact import finish_pending_retention, verify_retention
else:
    from capture import (
        code_page_guard,
        gpu_pids,
        gpu_state,
        idle_container,
        output,
        save,
    )
    from compact import finish_pending_retention, verify_retention


def interrupt(signum: int, _frame: object) -> None:
    raise InterruptedError(f"Experiment interrupted by signal {signum}")


def process_identity(pid: int) -> dict | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError:
        return None
    return {
        "pid": pid,
        "start_time": fields[19],
        "session_id": int(fields[3]),
        "state": fields[0],
    }


def signal_reader(identity: dict, signum: int) -> bool:
    """Signal only the exact saved host reader; never its shared process group."""
    current = process_identity(identity["pid"])
    if current is None or current["state"] == "Z":
        return False
    if any(current[key] != identity[key] for key in ("start_time", "session_id")):
        raise RuntimeError(f"Refusing to signal reused reader PID {identity['pid']}")
    os.kill(identity["pid"], signum)
    return True


def cleanup_capture_container(args: argparse.Namespace, run: Path) -> dict:
    """Freeze and release only descendants of verified owned container roots."""
    code = """
import json,os,signal,sys,time
from pathlib import Path
root=Path(sys.argv[1]); refused=[]; seeds={}
def state(pid):
    try:
        fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    except FileNotFoundError:
        return None
    return {'pid':pid,'ppid':int(fields[1]),'session':int(fields[3]),
            'start':fields[19],'state':fields[0]}
def accept(pid,started,session):
    current=state(pid)
    if current is not None:
        if current['start']!=str(started) or current['session']!=session:
            refused.append(pid)
        else:
            seeds[pid]=current
launcher=root/'gates/launcher.pid'
if launcher.exists():
    pid=int(launcher.read_text())
    saved=(root/'gates/launcher.stat').read_text().rsplit(')',1)[1].split()
    accept(pid,saved[19],pid)
ready=root/'gates/ready.json'
if ready.exists():
    saved=json.loads(ready.read_text())
    for pid,started in saved['starts'].items():
        accept(int(pid),started,saved['api'])
table={}
for path in Path('/proc').glob('[0-9]*'):
    item=state(int(path.name))
    if item is not None:
        table[item['pid']]=item
owned={}
for pid,saved in seeds.items():
    current=table.get(pid)
    if current and all(current[k]==saved[k] for k in ('start','session')):
        owned[pid]=saved
    elif current:
        refused.append(pid)
while True:
    children={pid:item for pid,item in table.items() if item['ppid'] in owned}
    if children.keys()<=owned.keys():
        break
    owned.update(children)
def remaining():
    rows=[]
    for pid,saved in owned.items():
        current=state(pid)
        if current and all(current[k]==saved[k] for k in ('start','session')):
            rows.append(current)
    return rows
groups=sorted((pid for pid,row in owned.items() if row['session']==pid),reverse=True)
for pid in groups:
    current=state(pid)
    if current and all(current[k]==owned[pid][k] for k in ('start','session')):
        try:
            os.killpg(pid,signal.SIGTERM)
        except ProcessLookupError:
            pass  # Exact owned process exited between identity read and signal.
deadline=time.monotonic()+30
while any(row['state']!='Z' for row in remaining()) and time.monotonic()<deadline:
    time.sleep(.2)
for row in remaining():
    if row['state']!='Z':
        current=state(row['pid'])
        if not current or any(current[k]!=row[k] for k in ('start','session')):
            continue
        try:
            os.kill(row['pid'],signal.SIGKILL)
        except ProcessLookupError:
            pass  # Exact owned process exited between identity read and signal.
print(json.dumps({'owned':list(owned.values()),'remaining':remaining(),'refused':refused}))
"""
    return json.loads(
        output(
            ["docker", "exec", args.container, args.python, "-c", code, str(run)],
            timeout=45,
        )
    )


def capture_timeout_cleanup(
    args: argparse.Namespace, root: Path, process: subprocess.Popen
) -> None:
    """Persist failure before bounded, identity-checked emergency cleanup."""
    run = root / "spe"
    evidence_path = root / "evidence/capture_shutdown.json"
    evidence: dict = {
        "status": "fail",
        "reason": "SPE capture did not finish cleanup within 240 seconds",
        "capture_pid": process.pid,
        "capture_identity": process_identity(process.pid),
        "started_ns": time.time_ns(),
        "errors": [],
    }
    save(evidence_path, evidence)
    gates = run / "gates"
    if gates.is_dir():
        (gates / "abort").touch()
    try:
        evidence["container"] = cleanup_capture_container(args, run)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        evidence["errors"].append(f"container cleanup: {exc}")
    readers_path = run / "evidence/readers.json"
    evidence["reader_identity_available"] = readers_path.exists()
    readers = []
    try:
        if readers_path.exists():
            readers = json.loads(readers_path.read_text())
        for signum, wait_seconds in (
            (signal.SIGINT, 15),
            (signal.SIGTERM, 5),
            (signal.SIGKILL, 5),
        ):
            active = []
            for reader in readers:
                try:
                    signaled = signal_reader(reader, signum)
                except (OSError, KeyError, RuntimeError) as exc:
                    evidence["errors"].append(f"reader cleanup: {exc}")
                    continue
                if signaled:
                    active.append(reader)
            if not active:
                break
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                if not any(
                    (current := process_identity(reader["pid"]))
                    and current["start_time"] == reader["start_time"]
                    and current["state"] != "Z"
                    for reader in active
                ):
                    break
                time.sleep(0.2)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        evidence["errors"].append(f"reader cleanup: {exc}")
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as exc:
        evidence["errors"].append(f"capture process remains: {exc}")
    evidence["readers_after"] = [process_identity(row["pid"]) for row in readers]
    evidence["capture_returncode"] = process.returncode
    evidence["finished_ns"] = time.time_ns()
    save(evidence_path, evidence)


def run_capture(command: list[str], args: argparse.Namespace, root: Path) -> None:
    """Let capture's finally run when this supervisor is interrupted."""
    # A terminal SIGINT must not reach capture twice while its finally is running.
    process = subprocess.Popen(command, start_new_session=True)
    try:
        result = process.wait()
        if result:
            raise subprocess.CalledProcessError(result, command)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=240)
            except subprocess.TimeoutExpired as exc:
                capture_timeout_cleanup(args, root, process)
                raise RuntimeError(
                    "SPE capture shutdown timed out; see evidence/capture_shutdown.json"
                ) from exc


def source_identity(args: argparse.Namespace, container: dict) -> dict:
    paths = [args.project / "kperf_instrument.py"]
    paths += sorted((args.project / "vllm").rglob("*.py"))
    paths += sorted((args.project / "scripts").rglob("*.py"))
    paths += sorted((args.project / "scripts").rglob("*.sh"))
    paths += sorted((args.project / "scripts").rglob("*.json"))
    paths += sorted((args.project / "scripts").rglob("*.c"))
    if args.pages:
        paths += [
            args.project / "scripts/pages" / name
            for name in ("namespace_exec", "prefault", "process_pages")
        ]
    paths += [args.project / "scripts/config.env"]
    identity = {
        "files": {
            str(path.relative_to(args.project)): {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in paths
        },
        "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
        "subreaper": {
            "path": str(args.reaper),
            "sha256": hashlib.sha256(args.reaper.read_bytes()).hexdigest(),
        },
        "container": {"id": container["Id"], "image": container["Image"]},
    }
    if args.pages:
        guard = code_page_guard(
            args.pages, {int(cpu) for cpu in args.cpus.split(",")}, int(args.node)
        )
        if guard:
            identity["code_page_guard"] = guard
    return identity


def verify_cleanup(args: argparse.Namespace, root: Path) -> dict:
    identities = []
    current_boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    for path in root.rglob("placement_identity.json"):
        if ".history" in path.relative_to(root).parts:
            continue
        boot_path = path.parent / "boot_id"
        if boot_path.exists() and boot_path.read_text().strip() != current_boot:
            continue
        identities.append(json.loads(path.read_text())["starts"])
    for path in root.rglob("benchmark_client/process.stat"):
        if ".history" in path.relative_to(root).parts:
            continue
        boot_path = path.parent.parent / "boot_id"
        if boot_path.exists() and boot_path.read_text().strip() != current_boot:
            continue
        text = path.read_text()
        fields = text.rsplit(")", 1)[1].split()
        identities.append({text.split(" ", 1)[0]: int(fields[19])})
    code = """
import json,sys
from pathlib import Path
remaining=[]
for identity in json.loads(sys.argv[1]):
    for pid,started in identity.items():
        try:
            fields=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        except FileNotFoundError:
            continue
        if int(fields[19])==started:
            remaining.append({'pid':int(pid),'start_time':started,'state':fields[0]})
print(json.dumps(remaining))
"""
    processes = json.loads(
        output(
            [
                "docker",
                "exec",
                args.container,
                args.python,
                "-c",
                code,
                json.dumps(identities),
            ]
        )
    )
    readers = []
    metadata = root / "spe/spe_capture.json"
    if metadata.exists():
        for entry in json.loads(metadata.read_text())["perf_files"]:
            reader = entry["reader"]
            try:
                fields = (
                    Path(f"/proc/{reader['pid']}/stat")
                    .read_text()
                    .rsplit(")", 1)[1]
                    .split()
                )
            except FileNotFoundError:
                continue
            if fields[19] == reader["start_time"]:
                readers.append(reader["pid"])
    pids = gpu_pids()
    result = {
        "status": "fail" if processes or readers or pids else "pass",
        "owned_processes_remaining": processes,
        "perf_readers_remaining": readers,
        "gpu_pids": pids,
    }
    save(root / "evidence/cleanup.json", result)
    if result["status"] != "pass":
        raise RuntimeError(f"Owned experiment resources remain: {result}")
    return result


def stop_owned(args: argparse.Namespace, monitor: Path) -> None:
    code = """
import json, os, signal, sys
from pathlib import Path
root=Path(sys.argv[1]); pidfile=root/'launcher.pid'
if pidfile.exists():
    pid=int(pidfile.read_text())
    saved=(root/'launcher.stat').read_text().rsplit(')',1)[1].split()
    try:
        current=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    except FileNotFoundError:
        current=None
    if current is not None:
        if current[19]!=saved[19] or int(current[3])!=pid:
            raise RuntimeError('Refusing to stop a reused/non-session PID')
        # Signal the shell once. Signaling tini's whole group also lets tini
        # forward a second TERM, interrupting the shell's cleanup traps.
        children=Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
        for child in children:
            try:
                fields=Path(f'/proc/{child}/stat').read_text().rsplit(')',1)[1].split()
            except FileNotFoundError:
                continue
            if int(fields[1])!=pid or int(fields[3])!=pid:
                raise RuntimeError('Refusing to stop a non-owned runner')
            os.kill(int(child),signal.SIGTERM)
"""
    subprocess.run(
        ["docker", "exec", args.container, args.python, "-c", code, str(monitor)],
        check=True,
        timeout=20,
    )


def monitor_process(
    process: subprocess.Popen,
    args: argparse.Namespace,
    monitor: Path,
    container_namespace: str,
) -> None:
    with (monitor / "thermal.jsonl").open("a") as stream:
        while process.poll() is None:
            state = gpu_state()
            pids = gpu_pids()
            stream.write(
                json.dumps({"time_ns": time.time_ns(), "gpus": state, "gpu_pids": pids})
                + "\n"
            )
            stream.flush()
            if args.max_temperature > 0 and any(
                row["temperature"] >= args.max_temperature for row in state
            ):
                raise RuntimeError("GPU reached experiment thermal stop threshold")
            for pid in pids:
                try:
                    namespace = os.readlink(f"/proc/{pid}/ns/pid")
                except FileNotFoundError:
                    continue  # A finishing GPU process disappeared during monitoring.
                if namespace != container_namespace:
                    raise RuntimeError(
                        "Another GPU workload appeared; stopping only our run"
                    )
            time.sleep(2)
    if process.returncode:
        raise RuntimeError(f"Topdown runner exited with code {process.returncode}")


def archive_result(path: Path) -> None:
    if not path.exists():
        return
    history = path.parent / ".history" / path.name
    history.mkdir(parents=True, exist_ok=True)
    number = 1
    while (history / str(number)).exists():
        number += 1
    path.rename(history / str(number))


def resume_state(
    args: argparse.Namespace, root: Path, monitor: Path, frozen: dict
) -> None:
    """Match scientific inputs before reusing rounds, preserving prior provenance."""
    contract_path = root.parent / f".{root.name}_resume_contract.json"
    fields = [
        "MODEL",
        "VLLM_SITE",
        "VLLM_VERSION",
        "VLLM_USE_V2_MODEL_RUNNER",
        "GPU_ID",
        "BLOCK_SIZE",
        "MAX_MODEL_LEN",
        "MAX_NUM_SEQS",
        "MAX_NUM_BATCHED_TOKENS",
        "TENSOR_PARALLEL_SIZE",
        "DATA_PARALLEL_SIZE",
        "DTYPE",
        "GPU_MEMORY_UTILIZATION",
        "PREFIX_CACHING_FLAG",
        "SERVER_SEED",
        "SERVER_FLAGS",
        "RANDOM_INPUT_LEN",
        "RANDOM_OUTPUT_LEN",
        "RANDOM_RANGE_RATIO",
        "NUM_PROMPTS",
        "NUM_WARMUPS",
        "MAX_CONCURRENCY",
        "REQUEST_RATE",
        "IGNORE_EOS_FLAG",
        "TEMPERATURE",
        "BENCH_SEED",
        "WORKER_CPUS",
        "WORKER_POOL_CPUS",
        "WORKER_NUMA_NODE",
        "SERVICE_CPUS",
        "CLIENT_CPUS",
        "HOTSPOT_SCOPE",
        "ROUND_WARMUPS",
        "WARMUP_SCOPE",
        "CODE_PAGE_CONDITION",
        "CODE_PAGE_MODE",
    ]

    fields += [
        "COLLECTION_PROFILE",
        "SPE_ENABLE",
        "FREQUENCY_ENABLE",
        "PERF_EVENT",
        "PERF_PERIOD",
        "CODE_PAGE_COVERAGE_POLICY",
        "CODE_PAGE_RESIDENCY_POLICY",
        "CODE_PAGE_AUDIT_MODE",
    ]
    fields += [
        line.split("=", 1)[0]
        for line in (args.project / "scripts/config.env").read_text().splitlines()
        if line.startswith("EVENTS_950_")
    ]

    def values(config: Path) -> dict:
        command = [
            "bash",
            "-c",
            (
                'set -a; source "$1"; shift; for key; do '
                'printf "%s=%s\\0" "$key" "${!key-}"; done'
            ),
            "config",
            str(args.project / "scripts/config.env"),
            *fields,
        ]
        env = {key: value for key, value in os.environ.items() if key not in fields}
        env["TOPDOWN_CONFIG"] = str(config)
        text = subprocess.check_output(command, env=env).decode()
        result = dict(item.split("=", 1) for item in text.split("\0") if item)
        if result.get("SPE_ENABLE") == "auto":
            result["SPE_ENABLE"] = "1"
        if not result.get("CODE_PAGE_CONDITION"):
            for key in (
                "CODE_PAGE_MODE",
                "CODE_PAGE_COVERAGE_POLICY",
                "CODE_PAGE_RESIDENCY_POLICY",
                "CODE_PAGE_AUDIT_MODE",
            ):
                result[key] = ""
        return result

    measurement_files = {
        name: record["sha256"]
        for name, record in frozen["files"].items()
        if name.startswith("vllm/")
        or name == "kperf_instrument.py"
        or name.endswith("/summary.py")
        or name.endswith("/report_config.json")
        or name == "scripts/parse_run.py"
    }
    contract = {
        "config": values(args.config),
        "measurement_files": measurement_files,
        "container": frozen["container"],
    }
    if contract_path.exists():
        if json.loads(contract_path.read_text()) != contract:
            raise ValueError(
                "Measurement inputs changed; restore configuration before resuming"
            )
    elif root.exists():
        raise ValueError("No saved configuration contract for this result directory")
    save(contract_path, contract)
    archive_result(monitor)


def run(args: argparse.Namespace) -> None:
    started = time.time_ns()
    root = args.run.resolve()
    if root.exists() and not args.resume:
        raise FileExistsError(root)
    if gpu_pids():
        raise RuntimeError("GPU already has a compute workload")
    if args.max_temperature > 0 and any(
        row["temperature"] >= 70 for row in gpu_state()
    ):
        raise RuntimeError("GPU must cool below 70C before starting this comparison")
    identity = json.loads(output(["docker", "inspect", args.container]))[0]
    if not idle_container(identity):
        raise RuntimeError("Expected inspected idle owned container")
    namespace = os.readlink(f"/proc/{identity['State']['Pid']}/ns/pid")
    root.parent.mkdir(parents=True, exist_ok=True)
    monitor = root.parent / ".monitor" / root.name
    frozen_source = source_identity(args, identity)
    if args.resume:
        resume_state(args, root, monitor, frozen_source)
        if (root / "complete.json").exists():
            receipt = json.loads((root / "complete.json").read_text())
            if Path(receipt["report"]).is_file() and (
                not args.pages
                or (
                    (root / "acceptance.json").exists()
                    and json.loads((root / "acceptance.json").read_text())["status"]
                    == "pass"
                )
            ):
                verify_cleanup(args, root)
                sys.stdout.write(receipt["report"] + "\n")
                return
    if args.resume:
        for path in root.glob("*.xlsx"):
            archive_result(path)
        archive_result(root / "complete.json")
        archive_result(root / "acceptance.json")
        archive_result(root / "evidence")
        for path in root.rglob("runtime_identity/*.json"):
            if ".history" not in path.relative_to(root).parts:
                captured = json.loads(path.read_text()).get("captured_ns")
                if captured:
                    started = min(started, int(captured))
    monitor.mkdir(parents=True, exist_ok=True)
    save(monitor / "source_identity.json", frozen_source)
    shutil.copyfile(args.config, monitor / "config.env")
    if args.pages:
        shutil.copyfile(args.pages / "identity.json", monitor / "page_identity.json")
        shutil.copyfile(args.pages / "prefault.tsv", monitor / "prefault.tsv")
    command = [
        "docker",
        "exec",
        "-w",
        "/tmp",
        "-e",
        f"TOPDOWN_CONFIG={args.config}",
        "-e",
        f"RUN_ROOT={root}",
        "-e",
        f"RESUME_COLLECTION={int(args.resume)}",
        "-e",
        f"TOPDOWN_SUPERVISOR_COMMAND={monitor / 'command.json'}",
        "-e",
        f"COLLECTION_PROFILE={args.profile}",
        "-e",
        "CODE_PARENT_MOUNT_NAMESPACE="
        + os.readlink(f"/proc/{identity['State']['Pid']}/ns/mnt"),
        *(
            ["-e", f"CODE_PAGE_GUARD={frozen_source['code_page_guard']['path']}"]
            if "code_page_guard" in frozen_source
            else []
        ),
        args.container,
        "setsid",
        "--wait",
        "bash",
        "-c",
        (
            'printf "%s\\n" "$$" > "$1/launcher.pid"; '
            'cat "/proc/$$/stat" > "$1/launcher.stat"; shift; exec "$@"'
        ),
        "experiment",
        str(monitor),
        str(args.reaper),
        "-s",
        "-g",
        "--",
    ]
    if args.pages:
        union = sorted(
            {
                int(cpu)
                for group in (args.cpus, args.pool, args.service, args.client)
                for cpu in group.split(",")
            }
        )
        command += [
            "numactl",
            "--physcpubind=" + ",".join(map(str, union)),
            f"--membind={args.node}",
            str(args.project / "scripts/pages/namespace_exec"),
            str(args.pages / "paths.txt"),
            str(args.pages / "files"),
        ]
    command += ["bash", str(args.project / "scripts" / args.chip / "run.sh")]
    save(monitor / "command.json", command)
    with (monitor / "runner.log").open("w") as stream:
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT)
        try:
            monitor_process(process, args, monitor, namespace)
        finally:
            if process.poll() is None:
                stop_owned(args, monitor)
                process.wait(timeout=90)
            if root.exists():
                evidence = root / "evidence"
                evidence.mkdir(exist_ok=True)
                shutil.copytree(monitor, evidence / "supervisor", dirs_exist_ok=True)
                save(evidence / "source_identity.json", frozen_source)
                shutil.copyfile(monitor / "config.env", evidence / "config.env")
                if args.pages:
                    shutil.copyfile(
                        monitor / "page_identity.json", evidence / "page_identity.json"
                    )
                    shutil.copyfile(monitor / "prefault.tsv", evidence / "prefault.tsv")
                if process.returncode:
                    verify_cleanup(args, root)
    reports = list(root.glob("*.xlsx"))
    if len(reports) != 1:
        raise RuntimeError("Expected one canonical Topdown workbook")
    scripts = args.project / "scripts/spe"
    if args.spe:
        capture = [
            sys.executable,
            str(scripts / "capture.py"),
            "--run",
            str(root / "spe"),
            "--container",
            args.container,
            "--reaper",
            str(args.reaper),
            "--config",
            str(args.config),
            "--cpus",
            args.cpus,
            "--binary-cache",
            str(args.binary_cache),
            "--max-temperature",
            str(args.max_temperature),
            "--python",
            args.python,
            "--gpu",
            args.gpu,
            "--node",
            args.node,
            "--skip-perf-dumps",
            "--raw-retention",
            "selected",
        ]
        if args.pages:
            capture += [
                "--namespace-manifest",
                str(args.pages),
                "--launch-cpus",
                ",".join(map(str, union)),
            ]
        if not args.resume or not (root / "spe/capture_complete.json").exists():
            if args.resume:
                archive_result(root / "spe")
            run_capture(capture, args, root)
        retained = root / "spe/analysis/retention.json"
        if args.resume and retained.exists():
            if not json.loads(retained.read_text()).get("deletion_complete"):
                finish_pending_retention(root / "spe")
            verify_retention(root / "spe")
        else:
            native_library = root / "spe/evidence/fast_scan.so"
            subprocess.run(
                [
                    "cc",
                    "-O3",
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-shared",
                    "-fPIC",
                    str(scripts / "fast_scan.c"),
                    "-o",
                    str(native_library),
                ],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(scripts / "decode.py"),
                    "--run-dir",
                    str(root / "spe"),
                    "--native-library",
                    str(native_library),
                ],
                check=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(scripts / "compact.py"),
                    "--run",
                    str(root / "spe"),
                    "--discard-raw",
                ],
                check=True,
            )
        subprocess.run(
            [
                "docker",
                "exec",
                args.container,
                args.python,
                str(scripts / "report.py"),
                "--workbook",
                str(reports[0]),
                "--spe-dir",
                str(root / "spe/analysis"),
            ],
            check=True,
        )
    if source_identity(args, identity) != frozen_source:
        raise RuntimeError("Collection source/config changed while this run was active")
    cleanup = verify_cleanup(args, root)
    save(
        root / "complete.json",
        {
            "started_ns": started,
            "finished_ns": time.time_ns(),
            "sessions": [
                str(path.relative_to(root))
                for path in root.rglob("service/placement_identity.json")
                if ".history" not in path.relative_to(root).parts
            ],
            "spe": args.spe,
            "report": str(reports[0]),
            "gpu_pids": cleanup["gpu_pids"],
            "config": str(args.config),
            "pages": str(args.pages) if args.pages else None,
            "page_evidence_policy": "observe" if args.observe_pages else "strict",
        },
    )
    if args.pages:
        subprocess.run(
            [
                sys.executable,
                str(args.project / "scripts/audit_run.py"),
                "--run-dir",
                str(root),
            ],
            check=True,
        )
    sys.stdout.write(str(reports[0]) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--reaper", type=Path, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--chip", default="920b")
    parser.add_argument(
        "--profile", default="end_to_end", choices=("full", "end_to_end")
    )
    parser.add_argument("--cpus", required=True)
    parser.add_argument("--pool", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--client", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--pages", type=Path)
    parser.add_argument("--observe-pages", action="store_true")
    parser.add_argument("--spe", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--binary-cache", type=Path, required=True)
    parser.add_argument("--max-temperature", type=int, default=85)
    parser.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(args)


if __name__ == "__main__":
    main()
