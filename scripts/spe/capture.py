# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Host-side SPE supervisor for the exact owned container Worker and CPU set."""

import argparse
import ctypes
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

LOGGER = logging.getLogger(__name__)
EVENT = "arm_spe_0/load_filter=1,store_filter=1,jitter=1,ts_enable=1,pa_enable=1/u"


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def output(command: list[str], timeout: int = 60) -> str:
    return subprocess.check_output(command, text=True, timeout=timeout)


def numa_python(code: str, arguments: list[str], cpus: set[int], node: int) -> str:
    """Read condition file bytes only with the Worker's CPU/NUMA placement."""
    if not cpus or any(cpu < 0 for cpu in cpus) or node < 0:
        raise ValueError("A nonempty Worker CPU set and NUMA node are required")
    return subprocess.check_output(
        [
            "numactl",
            f"--physcpubind={min(cpus)}",
            f"--membind={node}",
            sys.executable,
            "-c",
            code,
            *arguments,
        ],
        cwd=Path(__file__).resolve().parent,
        text=True,
        timeout=900,
    )


def code_page_guard(
    condition: Path | None, cpus: set[int], node: int
) -> dict[str, str] | None:
    """Validate an optional guard against its ordinary private ELF copy."""
    if condition is None:
        return None
    identity = json.loads((condition / "identity.json").read_text())
    if "code_page_guard" not in identity:
        return None
    guard = identity["code_page_guard"]
    if not isinstance(guard, dict):
        raise TypeError("Invalid code_page_guard descriptor")
    path, sha = guard.get("path"), guard.get("sha256")
    if (
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or str(Path(path)) != path
        or path.startswith("//")
        or ".." in Path(path).parts
        or any(char.isspace() or char in ":\x00" for char in path)
        or not isinstance(sha, str)
        or re.fullmatch("[0-9a-f]{64}", sha) is None
        or guard.get("method") != "executable_vma_madv_nohugepage"
    ):
        raise ValueError("Invalid code_page_guard path, SHA, or method")
    matches = [row for row in identity["files"] if row["path"] == path]
    if len(matches) != 1:
        raise ValueError("code_page_guard must match exactly one copied file")
    record = matches[0]
    segments = record.get("segments")
    if (
        record.get("task_generated_diagnostic", False)
        or record.get("sha256") != sha
        or not isinstance(record.get("size"), int)
        or record["size"] <= 0
        or not isinstance(segments, list)
        or not segments
        or any(
            not isinstance(segment, dict)
            or not isinstance(segment.get("offset"), int)
            or not isinstance(segment.get("size"), int)
            or segment["offset"] < 0
            or segment["size"] <= 0
            or segment["offset"] + segment["size"] > record["size"]
            for segment in segments
        )
    ):
        raise ValueError("code_page_guard needs matching ordinary ELF segment identity")
    copied = (record.get("copy_device"), record.get("copy_inode"))
    source = (record.get("source_device"), record.get("source_inode"))
    if (
        any(not isinstance(value, int) or value < 0 for value in (*copied, *source))
        or copied == source
    ):
        raise ValueError("code_page_guard source and copy must be independent")
    actual = condition.resolve() / "files" / path.lstrip("/")
    before = actual.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or actual.resolve() != actual
        or (before.st_dev, before.st_ino) != copied
        or before.st_size != record["size"]
    ):
        raise ValueError("code_page_guard copy identity, mode, or link count changed")
    digest = numa_python(
        "import hashlib,sys\n"
        "with open(sys.argv[1], 'rb') as source:\n"
        "    print(hashlib.file_digest(source, 'sha256').hexdigest())\n",
        [str(actual)],
        cpus,
        node,
    ).strip()
    after = actual.lstat()
    if digest != sha or any(
        getattr(before, field) != getattr(after, field)
        for field in (
            "st_dev",
            "st_ino",
            "st_size",
            "st_mode",
            "st_nlink",
            "st_mtime_ns",
        )
    ):
        raise ValueError("code_page_guard copy bytes or identity changed")
    return {"path": path, "sha256": sha, "method": guard["method"]}


def gpu_state() -> list[dict[str, int]]:
    rows = output(
        [
            "nvidia-smi",
            "--query-gpu=index,temperature.gpu,utilization.gpu,memory.used",
            "--format=csv,noheader,nounits",
        ]
    )
    return [
        dict(
            zip(
                ("index", "temperature", "utilization", "memory_mib"),
                (int(x.strip()) for x in row.split(",")),
                strict=True,
            )
        )
        for row in rows.splitlines()
        if row.strip()
    ]


def gpu_pids() -> list[int]:
    return [
        int(line)
        for line in output(
            [
                "nvidia-smi",
                "--query-compute-apps=pid",
                "--format=csv,noheader,nounits",
            ]
        ).splitlines()
        if line.strip()
    ]


def wait_gate(
    run: Path,
    name: str,
    process: subprocess.Popen,
    temperature: int,
    timeout: int = 900,
) -> None:
    deadline = time.monotonic() + timeout
    next_thermal = 0.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"SPE service exited before {name}: {process.returncode}"
            )
        if time.monotonic() >= next_thermal:
            state = gpu_state()
            with (run / "evidence/thermal.jsonl").open("a") as stream:
                stream.write(
                    json.dumps({"time_ns": time.time_ns(), "gpus": state}) + "\n"
                )
            if any(row["temperature"] >= temperature for row in state):
                raise RuntimeError("GPU reached the experiment thermal stop threshold")
            next_thermal = time.monotonic() + 5
        if (run / "gates" / name).exists():
            return
        time.sleep(0.2)
    raise TimeoutError(f"SPE gate timed out: {name}")


def host_worker(container_pid: int, container_worker: int) -> int:
    namespace = os.readlink(f"/proc/{container_pid}/ns/pid")
    matches = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            same = os.readlink(proc / "ns/pid") == namespace
            status = (proc / "status").read_text() if same else ""
        except (FileNotFoundError, ProcessLookupError):
            continue  # Processes may exit while enumerating; selected PID is verified.
        ids = re.search(r"^NSpid:\s+(.+)$", status, re.MULTILINE)
        if ids and int(ids[1].split()[-1]) == container_worker:
            matches.append(int(proc.name))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one namespace-matched Worker, got {matches}")
    if gpu_pids() != matches:
        raise RuntimeError("GPU ownership does not match this container Worker")
    return matches[0]


def snapshot(run: Path, pid: int, label: str, cpus: set[int]) -> None:
    target = run / "evidence" / label
    target.mkdir()
    proc = Path("/proc") / str(pid)
    for name in ("status", "stat", "maps", "smaps", "numa_maps", "cgroup", "sched"):
        (target / name).write_bytes((proc / name).read_bytes())
    threads = []
    for task in (proc / "task").iterdir():
        try:
            mask = os.sched_getaffinity(int(task.name))
        except ProcessLookupError:
            continue  # Completed helper thread; main Worker is checked separately.
        if mask != cpus:
            raise RuntimeError(f"Worker thread {task.name} escaped CPU set: {mask}")
        threads.append({"host_tid": int(task.name), "cpus": sorted(mask)})
    save(target / "threads.json", threads)


def binaries(run: Path, pid: int, cache: Path) -> list[dict[str, str]]:
    cache.mkdir(parents=True, exist_ok=True)
    root = Path(f"/proc/{pid}/root")
    rows = []
    paths = set()
    for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) == 6 and "x" in fields[1] and fields[5].startswith("/"):
            paths.add(fields[5])
    for mapped in sorted(paths):
        actual = root / mapped.lstrip("/")
        if not actual.is_file():
            rows.append({"mapped_path": mapped, "unavailable": "mapping file missing"})
            continue
        digest = hashlib.sha256()
        with actual.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        sha = digest.hexdigest()
        destination = cache / sha
        if not destination.exists():
            temporary = destination.with_suffix(".tmp")
            shutil.copyfile(actual, temporary)
            temporary.replace(destination)
        rows.append({"mapped_path": mapped, "path": str(destination), "sha256": sha})
    save(run / "evidence/binaries.json", rows)
    return [row for row in rows if "path" in row]


def snapshot_binaries(
    run: Path, pid: int, cache: Path, cpus: set[int], node: int
) -> list[dict[str, str]]:
    """Copy the live process-root binaries after formal windows, on its node."""
    return json.loads(
        numa_python(
            "import json,sys\n"
            "from pathlib import Path\n"
            "from capture import binaries\n"
            "print(json.dumps(binaries(Path(sys.argv[1]), int(sys.argv[2]), "
            "Path(sys.argv[3]))))\n",
            [str(run), str(pid), str(cache)],
            cpus,
            node,
        )
    )


def stop_reader(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
    try:
        result = process.wait(timeout=45)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        raise RuntimeError(
            "perf failed to flush after SIGINT; capture rejected"
        ) from None
    if result not in (0, 130, -signal.SIGINT):
        raise RuntimeError(f"perf recorder failed: {result}")


def stop_service(container: str, python: str, run: Path) -> None:
    code = """
import os,signal,sys
from pathlib import Path
root=Path(sys.argv[1]); path=root/'gates/launcher.pid'
if path.exists():
    pid=int(path.read_text())
    expected=(root/'gates/launcher.stat').read_text().rsplit(')',1)[1].split()
    try:
        current=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
    except FileNotFoundError:
        current=None
    if current is not None:
        if current[19]!=expected[19] or int(current[3])!=pid:
            raise RuntimeError('Refusing to stop reused/non-session PID')
        os.killpg(pid,signal.SIGTERM)
"""
    subprocess.run(
        ["docker", "exec", container, python, "-c", code, str(run)],
        check=True,
        timeout=20,
    )


def clock_pairs(run: Path, cpus: set[int], label: str) -> None:
    counter = ctypes.CDLL(str(run / "clock.so"))
    counter.read_counter.restype = ctypes.c_uint64
    counter.counter_frequency.restype = ctypes.c_uint64
    prior = os.sched_getaffinity(0)
    rows = []
    try:
        for cpu in sorted(cpus):
            os.sched_setaffinity(0, {cpu})
            for _ in range(8):
                before = time.monotonic_ns()
                ticks = counter.read_counter()
                after = time.monotonic_ns()
                rows.append(
                    {
                        "cpu": cpu,
                        "ticks": str(ticks),
                        "monotonic_before_ns": str(before),
                        "monotonic_after_ns": str(after),
                    }
                )
    finally:
        os.sched_setaffinity(0, prior)
    save(
        run / "evidence" / f"clock_{label}.json",
        {"cntfrq": counter.counter_frequency(), "pairs": rows},
    )


def resource_evidence(run: Path) -> None:
    block_size = int(
        Path("/sys/devices/system/memory/block_size_bytes").read_text(), 16
    )
    ranges = []
    for node in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        for block in node.glob("memory[0-9]*"):
            index = int(block.name.removeprefix("memory"))
            ranges.append(
                {
                    "start": index * block_size,
                    "end": (index + 1) * block_size,
                    "node": int(node.name.removeprefix("node")),
                }
            )
    save(
        run / "evidence/physical_numa.json",
        {"block_size": block_size, "ranges": ranges},
    )
    devices = []
    for device in sorted(Path("/sys/bus/pci/devices").iterdir()):
        devices.append(
            {
                "bdf": device.name,
                **{
                    name: (device / name).read_text().strip()
                    for name in ("vendor", "device", "class", "resource", "numa_node")
                    if (device / name).exists()
                },
            }
        )
    save(run / "evidence/pci_resources.json", devices)
    pmu = Path("/sys/bus/event_source/devices/arm_spe_0")
    paths = [
        pmu / "type",
        pmu / "cpumask",
        *sorted((pmu / "caps").glob("*")),
        *sorted((pmu / "format").glob("*")),
    ]
    save(
        run / "evidence/spe_sysfs.json",
        {
            str(path.relative_to(pmu)): path.read_text().strip()
            for path in paths
            if path.is_file()
        },
    )


def dump_perf(path: Path) -> dict[str, str]:
    raw = path.with_suffix(".packets.txt.gz")
    memory = path.with_suffix(".memory.txt.gz")
    commands = (
        (raw, ["perf", "script", "-D", "-i", str(path)]),
        (
            memory,
            [
                "perf",
                "script",
                "-i",
                str(path),
                "--itrace=M",
                "-F",
                "pid,tid,cpu,time,addr,data_src,weight,ip,phys_addr",
            ],
        ),
    )
    for destination, command in commands:
        error = destination.with_suffix(destination.suffix + ".stderr")
        temporary = destination.with_suffix(".tmp")
        with error.open("w") as err, temporary.open("wb") as stream:
            subprocess.run(command, stdout=stream, stderr=err, timeout=300, check=True)
        with (
            temporary.open("rb") as source,
            gzip.open(destination, "wb", compresslevel=1) as target,
        ):
            shutil.copyfileobj(source, target)
        temporary.unlink()
    return {
        "packets_text": str(raw),
        "memory_text": str(memory),
        "memory_format": "perf_memory_cpu",
    }


def capture(args: argparse.Namespace) -> None:
    started_ns = time.time_ns()
    run = args.run.resolve()
    if run.exists():
        raise FileExistsError(run)
    if gpu_pids():
        raise RuntimeError("Existing GPU compute workload; no service started")
    identity = json.loads(output(["docker", "inspect", args.container]))[0]
    if not identity["State"]["Running"] or identity["Config"]["Cmd"] != [
        "sleep",
        "infinity",
    ]:
        raise RuntimeError("Expected an already inspected owned idle container")
    cpus = {int(value) for value in args.cpus.split(",")}
    for name in ("data", "evidence", "gates", "runs"):
        (run / name).mkdir(parents=True, exist_ok=True)
    tools = Path(__file__).resolve().parent
    if args.node is None:
        raise ValueError("SPE clock preparation requires the Worker NUMA node")
    guard = code_page_guard(args.namespace_manifest, cpus, args.node)
    subprocess.run(
        [
            "numactl",
            f"--physcpubind={min(cpus)}",
            f"--membind={args.node}",
            "gcc",
            "-O2",
            "-shared",
            "-fPIC",
            str(tools / "clock.c"),
            "-o",
            str(run / "clock.so"),
        ],
        check=True,
    )
    if args.namespace_manifest:
        page_identity = json.loads(
            (args.namespace_manifest / "identity.json").read_text()
        )
        clock = run / "clock.so"
        clock_stat = clock.stat()
        page_identity["files"].append(
            {
                "path": str(clock),
                "copy_inode": clock_stat.st_ino,
                "copy_device": clock_stat.st_dev,
                "source_inode": clock_stat.st_ino,
                "source_device": clock_stat.st_dev,
                "sha256": hashlib.sha256(clock.read_bytes()).hexdigest(),
                "task_generated_diagnostic": True,
            }
        )
        save(run / "evidence/page_identity.json", page_identity)
    for name in ("iomem", "meminfo", "interrupts", "softirqs"):
        (run / "evidence" / f"{name}.txt").write_bytes(
            (Path("/proc") / name).read_bytes()
        )
    resource_evidence(run)
    bdf = (
        output(
            [
                "nvidia-smi",
                "--query-gpu=pci.bus_id",
                f"--id={args.gpu}",
                "--format=csv,noheader",
            ]
        )
        .strip()
        .lower()
    )
    bdf = f"{int(bdf.split(':')[0], 16):04x}:" + bdf.split(":", 1)[1]
    save(
        run / "evidence/container.json",
        {
            "id": identity["Id"],
            "image": identity["Image"],
            "pid": identity["State"]["Pid"],
            "cpus": identity["HostConfig"]["CpusetCpus"],
            "mems": identity["HostConfig"]["CpusetMems"],
        },
    )
    parent_mount = os.readlink(f"/proc/{identity['State']['Pid']}/ns/mnt")
    command = [
        "docker",
        "exec",
        "-e",
        f"TOPDOWN_CONFIG={args.config}",
        "-e",
        f"SPE_RUN={run}",
        "-e",
        f"CODE_PARENT_MOUNT_NAMESPACE={parent_mount}",
        *(["-e", f"CODE_PAGE_GUARD={guard['path']}"] if guard else []),
        args.container,
        "setsid",
        "--wait",
        "bash",
        "-c",
        (
            'printf "%s\\n" "$$" > "$1/gates/launcher.pid"; '
            'cat "/proc/$$/stat" > "$1/gates/launcher.stat"; shift; exec "$@"'
        ),
        "spe",
        str(run),
        str(args.reaper),
        "-s",
        "-g",
        "--",
    ]
    if args.namespace_manifest:
        if not args.launch_cpus or args.node is None:
            raise ValueError(
                "Private page namespace requires launch CPUs and NUMA node"
            )
        command += [
            "numactl",
            f"--physcpubind={args.launch_cpus}",
            f"--membind={args.node}",
            str(tools.parent / "pages/namespace_exec"),
            str(args.namespace_manifest / "paths.txt"),
            str(args.namespace_manifest / "files"),
        ]
    command += ["bash", str(tools / "run.sh")]
    save(run / "evidence/launch.json", command)
    readers: list[subprocess.Popen] = []
    worker = None
    with ExitStack() as stack:
        log = stack.enter_context((run / "collection.log").open("w"))
        service = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            wait_gate(run, "ready.json", service, args.max_temperature)
            placement = json.loads((run / "gates/ready.json").read_text())
            worker = host_worker(identity["State"]["Pid"], placement["worker"])
            snapshot(run, worker, "before_warmup", cpus)
            (run / "gates/record").touch()
            (run / "gates/go_0").touch()
            wait_gate(run, "done_0", service, args.max_temperature, 240)
            snapshot(run, worker, "before", cpus)
            clock_pairs(run, cpus, "before")
            raw_files = []
            path = run / "data" / "perf.data"
            command = [
                "perf",
                "record",
                "--no-inherit",
                "-t",
                str(worker),
                "--sample-cpu",
                "-e",
                EVENT,
                "-c",
                str(args.period),
                "-m",
                "64,4096",
                "-o",
                str(path),
            ]
            perf_log = stack.enter_context(path.with_suffix(".log").open("w"))
            record_started_ns = time.time_ns()
            reader = subprocess.Popen(
                command, stdout=perf_log, stderr=subprocess.STDOUT
            )
            readers.append(reader)
            reader_stat = (
                Path(f"/proc/{reader.pid}/stat").read_text().rsplit(")", 1)[1].split()
            )
            raw_files.append(
                {
                    "path": str(path),
                    "cpu": None,
                    "target_tid": worker,
                    "reader": {
                        "pid": reader.pid,
                        "start_time": reader_stat[19],
                        "session_id": int(reader_stat[3]),
                        "completed": False,
                    },
                }
            )
            reader_identity = run / "evidence/readers.json"
            temporary_identity = reader_identity.with_suffix(".tmp")
            save(temporary_identity, [entry["reader"] for entry in raw_files])
            temporary_identity.replace(reader_identity)
            save(path.with_suffix(".command.json"), command)
            time.sleep(1)
            if any(reader.poll() is not None for reader in readers):
                raise RuntimeError("SPE recorder failed before formal requests")
            for request in (1, 2, 3):
                (run / "gates" / f"go_{request}").touch()
                wait_gate(run, f"done_{request}", service, args.max_temperature, 240)
                if host_worker(identity["State"]["Pid"], placement["worker"]) != worker:
                    raise RuntimeError("Worker changed during SPE")
            for reader, entry in zip(readers, raw_files, strict=True):
                if reader.poll() is not None:
                    raise RuntimeError("SPE reader exited before controlled stop")
                entry["reader"]["alive_before_stop"] = True
                stop_reader(reader)
                entry["reader"].update(returncode=reader.returncode, completed=True)
            readers.clear()
            record_stopped_ns = time.time_ns()
            (run / "gates/recording_stopped").touch()
            wait_gate(run, "pages_done", service, args.max_temperature, 240)
            clock_pairs(run, cpus, "after")
            snapshot(run, worker, "after", cpus)
            binary_rows = snapshot_binaries(
                run, worker, args.binary_cache.resolve(), cpus, args.node
            )
            windows = []
            for request in (0, 1, 2, 3):
                path = run / "data" / f"windows_{placement['worker']}_{request}.json"
                windows.extend(json.loads(path.read_text()))
            save(run / "data/windows.json", windows)
            (run / "gates/complete").touch()
            if service.wait(timeout=90):
                raise RuntimeError("SPE service cleanup returned failure")
            if not args.skip_perf_dumps:
                for entry in raw_files:
                    entry.update(dump_perf(Path(entry["path"])))
            save(
                run / "spe_capture.json",
                {
                    "schema_version": 1,
                    "host_tid": worker,
                    "container_tid": placement["worker"],
                    "cpus": sorted(cpus),
                    "perf_files": raw_files,
                    "windows": "data/windows.json",
                    "maps_file": "evidence/before/maps",
                    "binaries": binary_rows,
                    "physical_numa": "evidence/physical_numa.json",
                    "pci_resources": "evidence/pci_resources.json",
                    "iomem": "evidence/iomem.txt",
                    "gpu_bdf": bdf,
                    "tools": {"objdump": "objdump", "readelf": "readelf"},
                    "counter_clock": "CNTVCT; no assumed monotonic offset",
                    "raw_retention": args.raw_retention,
                },
            )
            remaining = gpu_pids()
            if Path(f"/proc/{worker}").exists() or remaining:
                raise RuntimeError(
                    "SPE Worker/GPU resources remained after service cleanup"
                )
            save(
                run / "capture_complete.json",
                {
                    "finished_ns": time.time_ns(),
                    "started_ns": started_ns,
                    "record_started_ns": record_started_ns,
                    "record_stopped_ns": record_stopped_ns,
                    "record_seconds": (record_stopped_ns - record_started_ns) / 1e9,
                    "owned_worker_released": True,
                    "remaining_gpu_pids": remaining,
                    "perf_readers_complete": True,
                },
            )
        finally:
            (run / "gates/abort").touch()
            cleanup_errors = []
            for reader in readers:
                try:
                    stop_reader(reader)
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    cleanup_errors.append(str(exc))
                    LOGGER.error("SPE recorder cleanup: %s", exc)
            if service.poll() is None:
                try:
                    service.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    LOGGER.error(
                        "Stopping exact owned SPE launcher after gate abort timed out"
                    )
                    stop_service(args.container, args.python, run)
                    service.wait(timeout=60)
            if cleanup_errors:
                raise RuntimeError("; ".join(cleanup_errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--reaper", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cpus", required=True)
    parser.add_argument("--binary-cache", type=Path, required=True)
    parser.add_argument("--namespace-manifest", type=Path)
    parser.add_argument("--launch-cpus")
    parser.add_argument("--node", type=int)
    parser.add_argument("--python", default="/opt/vllm/bin/python3")
    parser.add_argument("--gpu", default="0")
    parser.add_argument("--period", type=int, default=1024)
    parser.add_argument("--raw-retention", choices=("all", "selected"), default="all")
    parser.add_argument("--max-temperature", type=int, default=85)
    parser.add_argument(
        "--skip-perf-dumps",
        action="store_true",
        help="Keep raw binary evidence without expanding full packet/memory text",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    def interrupted(signum: int, _frame: object) -> None:
        raise InterruptedError(f"SPE interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    capture(args)


if __name__ == "__main__":
    main()
