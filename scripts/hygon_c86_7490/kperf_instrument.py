from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import platform
import struct
import sys
import time

PERF_EVENT_OPEN = {"x86_64": 298}.get(platform.machine(), 298)
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
FDS: list[int] = []
IDS: list[int] = []
START_NS = 0
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


def init() -> None:
    if os.getenv("KPERF_ENABLE") != "1" or not EVENTS:
        return
    if len(EVENTS) > 5 or len(EVENTS) != len(NAMES):
        emit("[kperf-c86] init failed: require 1-5 events with matching names")
        return
    try:
        for index, event in enumerate(EVENTS):
            fd = open_event(event, FDS[0] if FDS else -1, index == 0)
            FDS.append(fd)
            IDS.append(event_id(fd))
    except OSError as error:
        close_group()
        emit(
            f"[kperf-c86] init failed: event=0x{event:04x} "
            f"errno={error.errno} {error.strerror}"
        )
        return
    emit(f"[kperf-c86] enabled: names={NAMES}, events={EVENTS}, fds={FDS}")


def kperf_begin(name: str) -> None:
    global ACTIVE, CALL, NAME, START_NS
    if not FDS:
        return
    try:
        fcntl.ioctl(FDS[0], PERF_IOC_RESET, PERF_IOC_FLAG_GROUP)
        fcntl.ioctl(FDS[0], PERF_IOC_ENABLE, PERF_IOC_FLAG_GROUP)
    except OSError as error:
        ACTIVE = False
        emit(f"[kperf-c86] begin failed: stage={name} errno={error.errno}")
        return
    START_NS = time.perf_counter_ns()
    CALL += 1
    NAME = name
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
    if not FDS or not ACTIVE:
        return
    end_ns = time.perf_counter_ns()
    ACTIVE = False
    try:
        fcntl.ioctl(FDS[0], PERF_IOC_DISABLE, PERF_IOC_FLAG_GROUP)
        time_enabled, time_running, counts = read_group()
        valid = int(time_running > 0 and time_running >= time_enabled)
    except OSError as error:
        emit(f"[kperf-c86] finish failed: stage={name} errno={error.errno}")
        time_enabled, time_running = 0, 0
        counts = [0] * len(FDS)
        valid = 0
    fields = [
        "KPERF_C86",
        name or NAME,
        str(CALL),
        f"{(end_ns - START_NS) / 1000:.2f}",
        str(time_enabled),
        str(time_running),
        str(valid),
        *(str(count) for count in counts),
    ]
    emit(",".join(fields))


init()
