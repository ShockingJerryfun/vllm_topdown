# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Native window scan with the existing packet normalizer for selected records."""

import ctypes
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

if __package__:
    from .records import (
        Chunk,
        PerfData,
        normalize_packets,
        read_packet,
        stream_bytes,
        time_ns,
    )
else:
    from records import (
        Chunk,
        PerfData,
        normalize_packets,
        read_packet,
        stream_bytes,
        time_ns,
    )


PACKETS = (
    "pad",
    "end",
    "alignment",
    "address",
    "counter",
    "timestamp",
    "event",
    "source",
    "context",
    "operation",
)
EXCLUSIONS = (
    "missing_timestamp",
    "outside_window",
    "missing_context",
    "other_thread",
    "warmup",
)


class Window(ctypes.Structure):
    _fields_ = [(field, ctypes.c_uint64) for field in ("start", "end", "formal")]


class Span(ctypes.Structure):
    _fields_ = [
        (field, ctypes.c_uint64) for field in ("start", "end", "ordinal", "terminal")
    ]


class Scanner:
    """Load an explicitly built scanner; never compile or silently fall back."""

    def __init__(self, library: Path) -> None:
        self.library = ctypes.CDLL(str(library.resolve()))
        self.library.spe_scan.argtypes = [
            ctypes.c_char_p,
            ctypes.c_uint64,
            ctypes.POINTER(Window),
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.POINTER(ctypes.POINTER(Span)),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_char_p,
            ctypes.c_uint64,
        ]
        self.library.spe_scan.restype = ctypes.c_int
        self.library.spe_scan_free.argtypes = [ctypes.c_void_p]
        self.library.spe_scan_free.restype = None

    def decode(
        self,
        perf: PerfData,
        chunks: list[Chunk],
        file_id: str,
        audit: Counter[str],
        exclusions: Counter[str],
        windows: list[dict],
        host_tid: int,
    ) -> Iterator[dict]:
        payload, segments = stream_bytes(perf, chunks)
        audit["alignment_padding_bytes"] += sum(
            s["previous_alignment_bytes_removed"] for s in segments
        )
        ranges = (Window * len(windows))(
            *(Window(w["start"], w["end"], w["request"] > 0) for w in windows)
        )
        spans = ctypes.POINTER(Span)()
        count = ctypes.c_uint64()
        totals = (ctypes.c_uint64 * 11)()
        skipped = (ctypes.c_uint64 * 5)()
        error = ctypes.create_string_buffer(256)
        result = self.library.spe_scan(
            payload,
            len(payload),
            ranges,
            len(ranges),
            host_tid,
            ctypes.byref(spans),
            ctypes.byref(count),
            totals,
            skipped,
            error,
            len(error),
        )
        if result:
            raise ValueError(error.value.decode())
        try:
            audit.update(
                {f"packet_{k}": totals[i] for i, k in enumerate(PACKETS) if totals[i]}
            )
            audit["records"] += totals[10]
            exclusions.update(
                {k: skipped[i] for i, k in enumerate(EXCLUSIONS) if skipped[i]}
            )
            ends = [segment["end"] for segment in segments]
            first = chunks[0]
            stream_id = f"{file_id}:{first.cpu}:{first.index}:{first.tid}"
            for i in range(count.value):
                span = spans[i]
                packets = []
                position = span.start
                while position < span.end:
                    segment = segments[bisect_right(ends, position)]
                    chunk = segment["chunk"]
                    local = position - segment["start"]
                    packet = read_packet(
                        payload,
                        position,
                        chunk.offset + local,
                        chunk.file_offset + local,
                    )
                    packets.append(packet)
                    position += len(packet.raw)
                row = normalize_packets(packets)
                terminal = segments[bisect_right(ends, span.terminal)]["chunk"]
                row.update(
                    record_id=f"{stream_id}:{span.ordinal}",
                    file=file_id,
                    stream_id=stream_id,
                    cpu=terminal.cpu,
                    aux_tid=terminal.tid,
                    aux_index=terminal.index,
                    aux_reference=str(terminal.reference),
                    time_ns=time_ns(int(row["ticks"]), terminal.conversion),
                )
                yield row
        finally:
            self.library.spe_scan_free(spans)
