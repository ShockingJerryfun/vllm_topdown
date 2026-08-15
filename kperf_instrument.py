# SPDX-License-Identifier: Apache-2.0
"""
kperf instrumentation module for CPU-side profiling.

This module provides cross-architecture PMU (Performance Monitoring Unit) collection
using perf_event_open(2) system calls, compatible with both aarch64 (Kunpeng) and x86_64 (AMD).

Environment Variables:
  KPERF_RAW_EVENTS: Comma-separated 64-bit raw PMU event codes
                    - Example: "0x001b,0x0070,0x0073,0x8005,0x0078"
                    - Default: "0x8,0x1,0x14,0x27,0x28" (Kunpeng cache_miss group)
  KPERF_EVENT_NAMES: Comma-separated event names for CSV output
  KPERF_PREFIX: Default prefix for CSV output (default: "home_w009")
  KPERF_TIME_MODE: "inner" (perf inside workload), "outer" (perf with ioctl overhead),
                   or "both" (default: both)

Usage:
    from kperf_instrument import kperf_begin, kperf_finish

    kperf_begin("my_function")
    # ... code to profile ...
    kperf_finish("my_function")

CSV Output Format:
    prefix,call,dur_inner,dur_outer,count1,count2,...
    - dur_inner/dur_outer appear based on KPERF_TIME_MODE
    - inner duration excludes ioctl overhead, outer includes it
"""

import ctypes
import errno
import fcntl
import os
import platform
import struct
import time

# perf_event_open 系统调用号(asm-generic/unistd.h: aarch64=241, x86_64=298)
_PERF_EVENT_OPEN_NR = {
    "aarch64": 241,
    "x86_64": 298,
}.get(platform.machine(), 241)  # 未知架构按 asm-generic 兜底
# perf_event_attr.type
_PERF_TYPE_RAW = 4  # 使用 raw 事件码,与原 libkperf type=99(custom)等价

# perf_event ioctl 请求码(include/uapi/linux/perf_event.h)
_PERF_EVENT_IOC_ENABLE = 0x2400
_PERF_EVENT_IOC_DISABLE = 0x2401
_PERF_EVENT_IOC_RESET = 0x2403

# perf_event_attr bitfield 位定义(只列用到几位)
# bit0:disabled, bit5:exclude_kernel, bit6:exclude_hv
_BIT_DISABLED = 1 << 0
_BIT_EXCLUDE_HV = 1 << 6   # ARM 上排除 hypervisor,避免异常计数


class _perf_event_attr(ctypes.Structure):
    """精简版 perf_event_attr,字段对齐内核 ABI(只用到 counting 场景)。

    布局必须与 include/uapi/linux/perf_event.h 一致(偏移以字节计):
      type(u32)@0 size(u32)@4 config(u64)@8 sample_period(u64)@16
      sample_type(u64)@24 read_format(u64)@32 _bitfield(u64)@40
      wakeup_events(u32)@48 bp_type(u32)@52 config1(u64)@56 config2(u64)@64
    总 size=72 == PERF_ATTR_SIZE_VER1,内核按此 size 解析。
    """
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample_period", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("_bitfield", ctypes.c_uint64),   # disabled/inherit/exclude_*/...
        ("wakeup_events", ctypes.c_uint32),
        ("bp_type", ctypes.c_uint32),
        ("config1", ctypes.c_uint64),     # 兼容内核 bp_addr/config1 联合
        ("config2", ctypes.c_uint64),     # 兼容内核 bp_len/config2 联合
    ]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.syscall.restype = ctypes.c_long
_libc.syscall.argtypes = [ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
                          ctypes.c_int, ctypes.c_int, ctypes.c_ulong]


def _perf_event_open(attr, pid, cpu, group_fd, flags):
    """包装 perf_event_open 系统调用,返回 fd(>=0)或 -1(失败)。"""
    return _libc.syscall(_PERF_EVENT_OPEN_NR, ctypes.byref(attr),
                         pid, cpu, group_fd, flags)


# ── 事件配置 ──
# 解析 KPERF_RAW_EVENTS 环境变量，格式: "0x001b,0x0070,0x0073,0x8005,0x0078"
_KPERF_RAW_EVENTS = os.getenv("KPERF_RAW_EVENTS", "").strip()
if _KPERF_RAW_EVENTS:
    _raw_tokens = [s.strip() for s in _KPERF_RAW_EVENTS.split(",") if s.strip()]
else:
    # 默认鲲鹏 topdown 组
    _raw_tokens = ["0x0011", "0x0008", "0x1f21", "0x001b"]

# 解析为 [(type, config), ...];config 按 64 位掩码处理
_KPERF_EVENT_SPECS = []
for tok in _raw_tokens:
    cfg = int(tok, 0) & 0xFFFFFFFFFFFFFFFF
    _KPERF_EVENT_SPECS.append((_PERF_TYPE_RAW, cfg))

_KPERF_PREFIX = os.getenv("KPERF_PREFIX", "home_w009")
_KPERF_EVENT_NAMES = [
    n.strip() for n in os.getenv("KPERF_EVENT_NAMES", "").split(",") if n.strip()
]
if not _KPERF_EVENT_NAMES:
    # 如果没有指定事件名称，自动生成为十六进制格式
    _KPERF_EVENT_NAMES = [f"0x{cfg:04x}" for _, cfg in _KPERF_EVENT_SPECS]

# 计时模式: inner=perf 内纯 workload(begin 循环后→finish 循环前)
#           outer=perf 外含全部 ioctl 开销(begin 循环前→finish 循环后)
#           both=两者都输出(默认)
_KPERF_TIME_MODE = os.getenv("KPERF_TIME_MODE", "both").strip().lower()
_KPERF_TIME_INNER = _KPERF_TIME_MODE in ("inner", "both")
_KPERF_TIME_OUTER = _KPERF_TIME_MODE in ("outer", "both")

_kperf_enabled = False
_kperf_fds = []            # 每个 raw 事件一个 perf_event fd(disabled 状态)
_kperf_time_inner_start = 0   # perf 内计时起点(begin 循环后)
_kperf_time_outer_start = 0   # perf 外计时起点(begin 循环前)
_kperf_call_count = 0
_kperf_cur_name = ""       # 本次采集归属的函数名(由 kperf_begin 记录)


def _kperf_init():
    """对每个事件 perf_event_open 一个 fd(pid=0 跟踪当前线程,cpu=-1 任意核)。
    fd 初始为 disabled,由 kperf_begin/kperf_finish 通过 ioctl 控制。"""
    global _kperf_enabled, _kperf_fds
    fds = []
    for idx, (etype, cfg) in enumerate(_KPERF_EVENT_SPECS):
        attr = _perf_event_attr()
        attr.size = ctypes.sizeof(_perf_event_attr)
        attr.type = etype
        attr.config = ctypes.c_uint64(cfg)
        # disabled=1:打开时默认不计数;exclude_hv=1:ARM 上排除 hypervisor
        # 注:libkperf 默认 useronly=0(user+kernel 都采),故此处 exclude_kernel=0
        attr._bitfield = _BIT_DISABLED | _BIT_EXCLUDE_HV
        attr.wakeup_events = 1
        attr.inherit = 1
        rc = _perf_event_open(attr, 0, -1, -1, 0)
        if rc < 0:
            # perf_event_open 失败(EINVAL=事件不支持;EACCES/EPERM=paranoid 太高)
            err = ctypes.get_errno()
            print(f"[kperf] perf_event_open #{idx} (cfg=0x{cfg:04x}) failed: errno={err} "
                  f"({errno.errorcode.get(err, 'unknown')})", flush=True)
            if err in (errno.EACCES, errno.EPERM):
                print("[kperf]   -> perf_event_paranoid 过高? "
                      "检查: cat /proc/sys/kernel/perf_event_paranoid "
                      "(raw 需 <=1 或 root)", flush=True)
            continue
        fds.append(int(rc))
    if fds:
        _kperf_fds = fds
        _kperf_enabled = True
        print(f"[kperf] enabled: prefix={_KPERF_PREFIX}, "
              f"time_mode={_KPERF_TIME_MODE}, events={_KPERF_EVENT_NAMES}, fds={fds}",
              flush=True)
    else:
        print(f"[kperf] init failed: all perf_event_open failed, "
              f"specs={_KPERF_EVENT_SPECS}", flush=True)


try:
    _kperf_init()
except Exception as _e:
    # perf_event_open 不可用时不影响正常执行
    print(f"[kperf] init exception: {_e}", flush=True)
    pass


def kperf_begin(name: str = ""):
    """开始采集:RESET 清零计数 + ENABLE 启用,记录两个计时起点。

    两个计时:
      outer: begin 循环前掐点 → 包含 perf ioctl 开销
      inner: begin 循环后掐点 → 纯 workload(perf 已 enable,误差仅 time_ns 本身)
    由 KPERF_TIME_MODE (inner/outer/both) 控制是否输出。

    :param name: 调用方函数名,仅用于 kperf_finish 输出时区分来源。
    """
    global _kperf_time_outer_start, _kperf_time_inner_start
    global _kperf_call_count, _kperf_cur_name
    if not _kperf_enabled:
        return
    try:
        if _KPERF_TIME_OUTER:
            _kperf_time_outer_start = time.time_ns()   # perf 外起点
        for fd in _kperf_fds:
            fcntl.ioctl(fd, _PERF_EVENT_IOC_RESET, 0)
            fcntl.ioctl(fd, _PERF_EVENT_IOC_ENABLE, 0)
        if _KPERF_TIME_INNER:
            _kperf_time_inner_start = time.time_ns()   # perf 内起点(= perf 已 enable)
        _kperf_call_count += 1
        _kperf_cur_name = name  # 记住本次采集归属的函数名
    except OSError:
        pass


def kperf_finish(name: str = ""):
    """结束采集:DISABLE 停用 + read 读计数,按 CSV 格式输出一行。

    inner 终点: finish 循环前掐点(perf 仍 on)
    outer 终点: finish 循环后掐点(含全部 DISABLE+read 开销)

    :param name: 调用方函数名,作为 CSV 第一列(prefix);为空时退回
                 kperf_begin 记录的名字,再为空则用全局 _KPERF_PREFIX。
    """
    if not _kperf_enabled:
        return
    if _KPERF_TIME_INNER:
        t_inner_end = time.time_ns()                  # perf 内终点(finish 循环前)
    counts = []
    for fd in _kperf_fds:
        try:
            fcntl.ioctl(fd, _PERF_EVENT_IOC_DISABLE, 0)
            data = os.read(fd, 8)
            counts.append(struct.unpack("<Q", data)[0] if len(data) == 8 else 0)
        except OSError:
            counts.append(0)
    if _KPERF_TIME_OUTER:
        t_outer_end = time.time_ns()                  # perf 外终点(finish 循环后)
    # CSV 格式: prefix,call,dur_inner,dur_outer,count1,count2,...
    # (dur_inner/dur_outer 按 KPERF_TIME_MODE 决定是否出现,inner 在前 outer 在后)
    prefix = name or _kperf_cur_name or _KPERF_PREFIX
    fields = [prefix, str(_kperf_call_count)]
    if _KPERF_TIME_INNER:
        fields.append(f"{(t_inner_end - _kperf_time_inner_start) / 1000.0:.2f}")
    if _KPERF_TIME_OUTER:
        fields.append(f"{(t_outer_end - _kperf_time_outer_start) / 1000.0:.2f}")
    fields.extend(str(c) for c in counts)
    print(",".join(fields), flush=True)


# For backward compatibility with existing code
_kperf_begin = kperf_begin
_kperf_finish = kperf_finish