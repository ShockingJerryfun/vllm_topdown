# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""SPE-only CNTVCT windows; the normal Topdown overlay never imports this module."""

import ctypes
import json
import os
import threading
from pathlib import Path

ROOT = Path(os.environ["SPE_RUN"])
COUNTER = ctypes.PyDLL(str(ROOT / "clock.so"))
COUNTER.read_counter.restype = ctypes.c_uint64
COUNTER.current_cpu.restype = ctypes.c_int
EXPECTED = int(os.environ["SPE_EXPECTED_CALLS"])
REQUEST = 0
ACTIVE = False
FULL: list[dict[str, object]] = []
REPLAY: list[tuple[int, int, int]] = []


def clock() -> tuple[int, int]:
    return COUNTER.read_counter(), COUNTER.current_cpu()


def recording() -> bool:
    global ACTIVE
    if not ACTIVE:
        ACTIVE = (ROOT / "gates/record").exists()
    return ACTIVE


def record_replay(start: tuple[int, int], end: tuple[int, int]) -> None:
    if recording():
        REPLAY.append((threading.get_native_id(), start[0], end[0]))


def record_full(start: tuple[int, int], end: tuple[int, int]) -> None:
    global REQUEST
    if not recording():
        return
    tid = threading.get_native_id()
    if len(REPLAY) != len(FULL) + 1:
        raise RuntimeError("SPE replay/fullgraph window count mismatch")
    replay = REPLAY[-1]
    if replay[0] != tid or not start[0] <= replay[1] < replay[2] <= end[0]:
        raise RuntimeError("SPE replay window nesting/thread mismatch")
    FULL.append(
        {
            "request": REQUEST,
            "step": len(FULL) + 1,
            "tid": tid,
            "start": str(start[0]),
            "end": str(end[0]),
            "replay_start": str(replay[1]),
            "replay_end": str(replay[2]),
            "cpu_start": start[1],
            "cpu_end": end[1],
        }
    )
    if len(FULL) == EXPECTED:
        destination = ROOT / "data" / f"windows_{os.getpid()}_{REQUEST}.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(FULL) + "\n")
        temporary.replace(destination)
        FULL.clear()
        REPLAY.clear()
        REQUEST += 1
