# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import statistics
import struct
import sys
import threading
import time
from pathlib import Path

PERF_EVENT_OPEN = {"aarch64": 241, "x86_64": 298}.get(platform.machine(), 241)
PERF_TYPE_RAW = 4
PERF_FORMAT_TOTAL_TIME_ENABLED = 1 << 0
PERF_FORMAT_TOTAL_TIME_RUNNING = 1 << 1
PERF_FORMAT_ID = 1 << 2
PERF_FORMAT_GROUP = 1 << 3
PERF_IOC_ENABLE = 0x2400
PERF_IOC_DISABLE = 0x2401
PERF_IOC_RESET = 0x2403
PERF_IOC_ID = 0x80082407
PERF_IOC_FLAG_GROUP = 1
PERF_FLAG_FD_CLOEXEC = 1 << 3
PERF_DISABLED = 1 << 0
PERF_PINNED = 1 << 2
PERF_EXCLUDE_HV = 1 << 6


class PerfEventAttr(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("wakeup_events", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),
        ("config2", ctypes.c_uint64),
    ]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
LIBC.syscall.argtypes = [
    ctypes.c_long,
    ctypes.c_void_p,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_ulong,
]
EVENTS = [
    int(code.strip(), 0)
    for code in os.getenv("KPERF_RAW_EVENTS", "").split(",")
    if code.strip()
]
NAMES = [
    name.strip()
    for name in os.getenv("KPERF_EVENT_NAMES", "").split(",")
    if name.strip()
]
MODE = os.getenv("KPERF_MODE", "pmu")
SCOPE = os.getenv("KPERF_SCOPE", "thread")
PMU_NAME = os.getenv("KPERF_PMU_NAME", "")
ENABLED = os.getenv("KPERF_ENABLE") == "1"
CONTROL_ENABLED = os.getenv("KPERF_CONTROL") == "1"
OWNER_TID = 0
TARGET_STAGE = os.getenv("KPERF_TARGET", "")
QUALIFIER_STAGE = os.getenv("KPERF_QUALIFIER", "")
STRICT_THREAD = os.getenv("KPERF_STRICT_NUMA") == "1"
STRICT_OWNER_TID = threading.get_native_id()
COUNTER_GROUPS: list[tuple[list[int], list[int]]] = []
GROUP_TIMES: dict[int, tuple[int, int]] = {}
WALL_START_NS = 0
THREAD_START_NS = 0
WALL_OVERHEAD_NS = 0
THREAD_OVERHEAD_NS = 0
CALL = 0
NAME = ""
ACTIVE = False
QUALIFIED = False
RUNTIME_RECORDED = False


def emit(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def event_id(fd: int) -> int:
    data = bytearray(8)
    fcntl.ioctl(fd, PERF_IOC_ID, data, True)
    return struct.unpack("<Q", data)[0]


def open_event(
    event: int,
    event_type: int,
    pid: int,
    cpu: int,
    group_fd: int,
    leader: bool,
    exclude_hv: bool,
) -> int:
    attr = PerfEventAttr()
    attr.type = event_type
    attr.size = ctypes.sizeof(PerfEventAttr)
    attr.config = event
    attr.read_format = (
        PERF_FORMAT_GROUP
        | PERF_FORMAT_TOTAL_TIME_ENABLED
        | PERF_FORMAT_TOTAL_TIME_RUNNING
        | PERF_FORMAT_ID
    )
    attr.flags = PERF_EXCLUDE_HV if exclude_hv else 0
    if leader:
        attr.flags |= PERF_DISABLED | PERF_PINNED
    fd = LIBC.syscall(
        PERF_EVENT_OPEN,
        ctypes.byref(attr),
        pid,
        cpu,
        group_fd,
        PERF_FLAG_FD_CLOEXEC,
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, errno.errorcode.get(error, "unknown"))
    return int(fd)


def open_group(
    event_type: int,
    pid: int,
    cpu: int,
    exclude_hv: bool,
) -> tuple[list[int], list[int]]:
    fds: list[int] = []
    ids: list[int] = []
    try:
        for index, event in enumerate(EVENTS):
            fd = open_event(
                event,
                event_type,
                pid,
                cpu,
                fds[0] if fds else -1,
                index == 0,
                exclude_hv,
            )
            fds.append(fd)
            ids.append(event_id(fd))
    except OSError:
        for fd in fds:
            os.close(fd)
        raise
    return fds, ids


def close_groups() -> None:
    for fds, _ in COUNTER_GROUPS:
        for fd in fds:
            os.close(fd)
    COUNTER_GROUPS.clear()
    GROUP_TIMES.clear()


def configure(
    mode: str,
    codes: str = "",
    names: str = "",
    scope: str = "thread",
    pmu_name: str = "",
    target: str = "",
    qualifier: str = "",
    round_id: str = "",
) -> dict[str, object]:
    """Switch an idle collector on the model execution thread, before a request."""
    global MODE, EVENTS, NAMES, SCOPE, PMU_NAME, ENABLED, OWNER_TID
    global TARGET_STAGE, QUALIFIER_STAGE, CALL, NAME, QUALIFIED
    if not CONTROL_ENABLED:
        raise RuntimeError("Runtime switching requires KPERF_CONTROL=1")
    check_owner("switch")
    tid = threading.get_native_id()
    if ACTIVE or (OWNER_TID and tid != OWNER_TID):
        raise RuntimeError("Switch requires an idle collector on its owner thread")
    if mode not in ("disabled", "time", "pmu"):
        raise ValueError(f"Unsupported collection mode: {mode}")
    events = [int(code.strip(), 0) for code in codes.split(",") if code.strip()]
    event_names = [name.strip() for name in names.split(",") if name.strip()]
    if mode == "pmu" and (not events or len(events) != len(event_names)):
        raise ValueError("PMU mode requires matching events and names")
    if scope not in ("thread", "uncore"):
        raise ValueError(f"Unsupported PMU scope: {scope}")
    if scope == "uncore" and not pmu_name:
        raise ValueError("Uncore mode requires a PMU name")

    ENABLED = False
    for fds, _ in COUNTER_GROUPS:
        fcntl.ioctl(fds[0], PERF_IOC_DISABLE, PERF_IOC_FLAG_GROUP)
    close_groups()
    OWNER_TID = tid
    MODE, EVENTS, NAMES = mode, events, event_names
    SCOPE, PMU_NAME = scope, pmu_name
    TARGET_STAGE, QUALIFIER_STAGE = target, qualifier
    CALL, NAME, QUALIFIED = 0, "", False
    if mode == "pmu":
        init_pmu()
        if not COUNTER_GROUPS:
            raise RuntimeError("Failed to open the requested PMU group")
    elif mode == "time":
        init_time()
    ENABLED = mode != "disabled"
    emit(f"[kperf] round={round_id} mode={mode} pid={os.getpid()} tid={tid}")
    return {
        "round_id": round_id,
        "pid": os.getpid(),
        "tid": tid,
        "mode": mode,
        "events": EVENTS,
        "names": NAMES,
        "event_ids": [ids for _, ids in COUNTER_GROUPS],
    }


def parse_cpu_list(value: str) -> list[int]:
    cpus: set[int] = set()
    for item in value.strip().split(","):
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if end < start:
                raise ValueError(f"invalid CPU range: {item}")
            cpus.update(range(start, end + 1))
        else:
            cpus.add(int(item))
    if not cpus:
        raise ValueError("CPU list is empty")
    return sorted(cpus)


def calibrate_time_overhead(samples: int = 257) -> tuple[int, int]:
    wall_deltas: list[int] = []
    thread_deltas: list[int] = []
    for _ in range(samples):
        thread_start = time.thread_time_ns()
        wall_start = time.perf_counter_ns()
        wall_end = time.perf_counter_ns()
        thread_end = time.thread_time_ns()
        wall_deltas.append(wall_end - wall_start)
        thread_deltas.append(thread_end - thread_start)
    return statistics.median_low(wall_deltas), statistics.median_low(thread_deltas)


def init_time() -> None:
    global THREAD_OVERHEAD_NS, WALL_OVERHEAD_NS
    WALL_OVERHEAD_NS, THREAD_OVERHEAD_NS = calibrate_time_overhead()
    emit(
        f"[kperf] enabled: mode=time wall_overhead_ns={WALL_OVERHEAD_NS} "
        f"thread_overhead_ns={THREAD_OVERHEAD_NS}"
    )


def init_pmu() -> None:
    if not EVENTS:
        emit("[kperf] init failed: PMU mode requires raw events")
        return
    if len(EVENTS) != len(NAMES):
        emit("[kperf] init failed: event names do not match event codes")
        return
    try:
        if SCOPE == "thread":
            COUNTER_GROUPS.append(
                open_group(PERF_TYPE_RAW, pid=0, cpu=-1, exclude_hv=True)
            )
            scope_detail = "thread"
        elif SCOPE == "uncore":
            if not PMU_NAME:
                raise ValueError("uncore scope requires KPERF_PMU_NAME")
            pmu_root = Path("/sys/bus/event_source/devices") / PMU_NAME
            event_type = int((pmu_root / "type").read_text(encoding="ascii").strip())
            cpus = parse_cpu_list((pmu_root / "cpumask").read_text(encoding="ascii"))
            for cpu in cpus:
                COUNTER_GROUPS.append(
                    open_group(
                        event_type,
                        pid=-1,
                        cpu=cpu,
                        exclude_hv=False,
                    )
                )
            scope_detail = f"uncore pmu={PMU_NAME} cpus={cpus}"
        else:
            raise ValueError(f"unsupported PMU scope={SCOPE}")
    except (OSError, ValueError) as error:
        close_groups()
        emit(
            f"[kperf] init failed: scope={SCOPE} error={type(error).__name__}: {error}"
        )
        return
    emit(
        f"[kperf] enabled: mode=pmu scope={scope_detail} "
        f"names={NAMES}, events={EVENTS}, groups={len(COUNTER_GROUPS)}"
    )


def init() -> None:
    if not ENABLED:
        return
    if MODE == "time":
        init_time()
    elif MODE == "pmu":
        init_pmu()
    else:
        emit(f"[kperf] init failed: unsupported mode={MODE}")


def check_owner(boundary: str) -> None:
    if not STRICT_THREAD:
        return
    tid = threading.get_native_id()
    if tid != STRICT_OWNER_TID:
        raise RuntimeError(f"{boundary}: probe TID {tid} != owner {STRICT_OWNER_TID}")


def record_runtime_identity() -> None:
    """Record imports inside the actual probed process, before any timed span."""
    global RUNTIME_RECORDED
    if RUNTIME_RECORDED:
        return
    directory = os.getenv("KPERF_RUNTIME_IDENTITY_DIR")
    if not directory:
        return
    modules = {}
    for name, module in sorted(list(sys.modules.items())):
        if not (
            name == "kperf_instrument"
            or name == "spe_marker"
            or name == "torch"
            or name == "vllm"
            or name.startswith("vllm.")
        ):
            continue
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve(strict=True)
        modules[name] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()
    value = {
        "pid": pid,
        "tid": threading.get_native_id(),
        "python": sys.executable,
        "version": sys.version,
        "sys_path": sys.path,
        "modules": modules,
        "stat": Path("/proc/self/stat").read_text(),
        "cpu_affinity": sorted(os.sched_getaffinity(0)),
        "captured_ns": time.time_ns(),
    }
    path = root / f"{pid}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
    RUNTIME_RECORDED = True


def kperf_begin(name: str) -> None:
    global ACTIVE, CALL, NAME, QUALIFIED, THREAD_START_NS, WALL_START_NS
    record_runtime_identity()
    if not ENABLED:
        return
    if TARGET_STAGE and name != TARGET_STAGE:
        if ACTIVE and name == QUALIFIER_STAGE:
            QUALIFIED = True
        return
    if CONTROL_ENABLED and threading.get_native_id() != OWNER_TID:
        raise RuntimeError("Probe executed outside the PMU owner thread")
    check_owner("begin")
    QUALIFIED = False
    if MODE == "time":
        CALL += 1
        NAME = name
        ACTIVE = True
        THREAD_START_NS = time.thread_time_ns()
        WALL_START_NS = time.perf_counter_ns()
        return
    if not COUNTER_GROUPS:
        return
    CALL += 1
    NAME = name
    try:
        enabled_leaders: list[int] = []
        for fds, _ in COUNTER_GROUPS:
            leader = fds[0]
            fcntl.ioctl(leader, PERF_IOC_RESET, PERF_IOC_FLAG_GROUP)
            fcntl.ioctl(leader, PERF_IOC_ENABLE, PERF_IOC_FLAG_GROUP)
            enabled_leaders.append(leader)
    except OSError as error:
        for leader in enabled_leaders:
            try:
                fcntl.ioctl(leader, PERF_IOC_DISABLE, PERF_IOC_FLAG_GROUP)
            except OSError as cleanup_error:
                emit(
                    "[kperf] begin cleanup failed: "
                    f"stage={name} errno={cleanup_error.errno}"
                )
        CALL -= 1
        ACTIVE = False
        emit(f"[kperf] begin failed: stage={name} errno={error.errno}")
        return
    ACTIVE = True


def read_group(fds: list[int], ids: list[int]) -> tuple[int, int, list[int]]:
    size = 24 + 16 * len(fds)
    data = os.read(fds[0], size)
    if len(data) != size:
        raise OSError(errno.EIO, "short group read")
    values = struct.unpack(f"<QQQ{2 * len(fds)}Q", data)
    if values[0] != len(fds):
        raise OSError(errno.EIO, "unexpected group size")
    time_enabled, time_running = values[1:3]
    by_id = dict(zip(values[4::2], values[3::2], strict=True))
    return time_enabled, time_running, [by_id.get(event, 0) for event in ids]


def kperf_finish(name: str) -> None:
    global ACTIVE, QUALIFIED
    if TARGET_STAGE and name != TARGET_STAGE:
        return
    if not ACTIVE:
        return
    check_owner("end")
    if MODE == "time":
        wall_end_ns = time.perf_counter_ns()
        thread_end_ns = time.thread_time_ns()
        ACTIVE = False
        wall_ns = max(0, wall_end_ns - WALL_START_NS - WALL_OVERHEAD_NS)
        thread_ns = max(
            0,
            thread_end_ns - THREAD_START_NS - THREAD_OVERHEAD_NS,
        )
        valid = int(wall_ns > 0)
        emit(
            ",".join(
                (
                    "KPERF_TIME",
                    name or NAME,
                    str(CALL),
                    str(wall_ns),
                    str(thread_ns),
                    str(valid),
                )
            )
        )
        if QUALIFIED:
            emit(f"KPERF_QUALIFIER,{NAME},{CALL},{QUALIFIER_STAGE}")
        QUALIFIED = False
        return
    if not COUNTER_GROUPS:
        ACTIVE = False
        QUALIFIED = False
        return
    disable_error: OSError | None = None
    for fds, _ in COUNTER_GROUPS:
        try:
            fcntl.ioctl(fds[0], PERF_IOC_DISABLE, PERF_IOC_FLAG_GROUP)
        except OSError as error:
            disable_error = disable_error or error
    try:
        ACTIVE = False
        if disable_error is not None:
            raise disable_error
        time_enabled = 0
        time_running = 0
        counts = [0] * len(EVENTS)
        valid = 1
        for fds, ids in COUNTER_GROUPS:
            group_enabled, group_running, group_counts = read_group(fds, ids)
            if CONTROL_ENABLED:
                previous = GROUP_TIMES.get(fds[0], (0, 0))
                GROUP_TIMES[fds[0]] = (group_enabled, group_running)
                group_enabled -= previous[0]
                group_running -= previous[1]
            time_enabled += group_enabled
            time_running += group_running
            valid &= int(group_running > 0 and group_running == group_enabled)
            counts = [
                total + count for total, count in zip(counts, group_counts, strict=True)
            ]
    except OSError as error:
        ACTIVE = False
        emit(f"[kperf] finish failed: stage={name} errno={error.errno}")
        time_enabled, time_running = 0, 0
        counts = [0] * len(EVENTS)
        valid = 0
    fields = [
        "KPERF",
        name or NAME,
        str(CALL),
        str(time_enabled),
        str(time_running),
        str(valid),
        *(str(count) for count in counts),
    ]
    emit(",".join(fields))
    if QUALIFIED:
        emit(f"KPERF_QUALIFIER,{NAME},{CALL},{QUALIFIER_STAGE}")
    QUALIFIED = False


def kperf_span_begin(name: str) -> None:
    if name == TARGET_STAGE:
        kperf_begin(name)


def kperf_span_finish(name: str) -> None:
    if name == TARGET_STAGE:
        kperf_finish(name)


if STRICT_THREAD and ENABLED:
    emit(
        f"[kperf] owner_pid={os.getpid()} owner_tid={STRICT_OWNER_TID} "
        "begin_end_owner_checks=required"
    )
init()
