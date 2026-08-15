import ctypes
import errno
import fcntl
import os
import platform
import struct
import time

PERF_EVENT_OPEN = {"aarch64": 241, "x86_64": 298}.get(platform.machine(), 241)
PERF_TYPE_RAW = 4
PERF_IOC_ENABLE = 0x2400
PERF_IOC_DISABLE = 0x2401
PERF_IOC_RESET = 0x2403
PERF_DISABLED = 1 << 0
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


LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
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
START_NS = 0
CALL = 0
NAME = ""


def init() -> None:
    if os.getenv("KPERF_ENABLE") != "1" or not EVENTS:
        return
    for index, event in enumerate(EVENTS):
        attr = PerfEventAttr()
        attr.type = PERF_TYPE_RAW
        attr.size = ctypes.sizeof(PerfEventAttr)
        attr.config = event
        attr.flags = PERF_DISABLED | PERF_EXCLUDE_HV
        fd = LIBC.syscall(PERF_EVENT_OPEN, ctypes.byref(attr), 0, -1, -1, 0)
        if fd < 0:
            error = ctypes.get_errno()
            for opened_fd in FDS:
                os.close(opened_fd)
            FDS.clear()
            print(
                f"[kperf] perf_event_open #{index} cfg=0x{event:04x} "
                f"failed errno={error} {errno.errorcode.get(error, 'unknown')}",
                flush=True,
            )
            return
        FDS.append(int(fd))
    print(f"[kperf] enabled: events={NAMES}, fds={FDS}", flush=True)


def kperf_begin(name: str) -> None:
    global START_NS, CALL, NAME
    if not FDS:
        return
    try:
        for fd in FDS:
            fcntl.ioctl(fd, PERF_IOC_RESET, 0)
            fcntl.ioctl(fd, PERF_IOC_ENABLE, 0)
        START_NS = time.time_ns()
        CALL += 1
        NAME = name
    except OSError:
        pass


def kperf_finish(name: str) -> None:
    if not FDS:
        return
    end_ns = time.time_ns()
    counts: list[int] = []
    for fd in FDS:
        try:
            fcntl.ioctl(fd, PERF_IOC_DISABLE, 0)
            data = os.read(fd, 8)
            counts.append(struct.unpack("<Q", data)[0] if len(data) == 8 else 0)
        except OSError:
            counts.append(0)
    fields = [name or NAME, str(CALL), f"{(end_ns - START_NS) / 1000:.2f}"]
    fields.extend(str(count) for count in counts)
    print(",".join(fields), flush=True)


init()
