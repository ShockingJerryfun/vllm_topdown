from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
import statistics
import struct
import sys
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
COUNTER_GROUPS: list[tuple[list[int], list[int]]] = []
WALL_START_NS = 0
THREAD_START_NS = 0
WALL_OVERHEAD_NS = 0
THREAD_OVERHEAD_NS = 0
CALL = 0
NAME = ""
ACTIVE = False


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


def kperf_begin(name: str) -> None:
    global ACTIVE, CALL, NAME, THREAD_START_NS, WALL_START_NS
    if not ENABLED:
        return
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
    global ACTIVE
    if not ACTIVE:
        return
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
        return
    if not COUNTER_GROUPS:
        ACTIVE = False
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


init()
