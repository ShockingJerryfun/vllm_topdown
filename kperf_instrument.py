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
ENABLED = os.getenv("KPERF_ENABLE") == "1"
FDS: list[int] = []
IDS: list[int] = []
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


def open_event(event: int, group_fd: int, leader: bool) -> int:
    attr = PerfEventAttr()
    attr.type = PERF_TYPE_RAW
    attr.size = ctypes.sizeof(PerfEventAttr)
    attr.config = event
    attr.read_format = (
        PERF_FORMAT_GROUP
        | PERF_FORMAT_TOTAL_TIME_ENABLED
        | PERF_FORMAT_TOTAL_TIME_RUNNING
        | PERF_FORMAT_ID
    )
    attr.flags = PERF_EXCLUDE_HV
    if leader:
        attr.flags |= PERF_DISABLED | PERF_PINNED
    fd = LIBC.syscall(
        PERF_EVENT_OPEN,
        ctypes.byref(attr),
        0,
        -1,
        group_fd,
        PERF_FLAG_FD_CLOEXEC,
    )
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, errno.errorcode.get(error, "unknown"))
    return int(fd)


def close_group() -> None:
    for fd in FDS:
        os.close(fd)
    FDS.clear()
    IDS.clear()


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
        for index, event in enumerate(EVENTS):
            fd = open_event(event, FDS[0] if FDS else -1, index == 0)
            FDS.append(fd)
            IDS.append(event_id(fd))
    except OSError as error:
        close_group()
        emit(
            f"[kperf] init failed: event=0x{event:04x} "
            f"errno={error.errno} {error.strerror}"
        )
        return
    emit(f"[kperf] enabled: mode=pmu names={NAMES}, events={EVENTS}, fds={FDS}")


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
    if not FDS:
        return
    CALL += 1
    NAME = name
    try:
        fcntl.ioctl(FDS[0], PERF_IOC_RESET, PERF_IOC_FLAG_GROUP)
        fcntl.ioctl(FDS[0], PERF_IOC_ENABLE, PERF_IOC_FLAG_GROUP)
    except OSError as error:
        CALL -= 1
        ACTIVE = False
        emit(f"[kperf] begin failed: stage={name} errno={error.errno}")
        return
    ACTIVE = True


def read_group() -> tuple[int, int, list[int]]:
    size = 24 + 16 * len(FDS)
    data = os.read(FDS[0], size)
    if len(data) != size:
        raise OSError(errno.EIO, "short group read")
    values = struct.unpack(f"<QQQ{2 * len(FDS)}Q", data)
    if values[0] != len(FDS):
        raise OSError(errno.EIO, "unexpected group size")
    time_enabled, time_running = values[1:3]
    by_id = dict(zip(values[4::2], values[3::2], strict=True))
    return time_enabled, time_running, [by_id.get(event, 0) for event in IDS]


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
    if not FDS:
        ACTIVE = False
        return
    try:
        fcntl.ioctl(FDS[0], PERF_IOC_DISABLE, PERF_IOC_FLAG_GROUP)
        ACTIVE = False
        time_enabled, time_running, counts = read_group()
        valid = int(time_running > 0 and time_running == time_enabled)
    except OSError as error:
        ACTIVE = False
        emit(f"[kperf] finish failed: stage={name} errno={error.errno}")
        time_enabled, time_running = 0, 0
        counts = [0] * len(FDS)
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
