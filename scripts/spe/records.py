# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Read perf AUX streams without mixing CPUs, indices, or task contexts."""

import struct
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MASK64 = (1 << 64) - 1
MASK56 = (1 << 56) - 1
EVENT_NAMES = {
    0: "EXCEPTION-GEN",
    1: "RETIRED",
    2: "L1D-ACCESS",
    3: "L1D-REFILL",
    4: "TLB-ACCESS",
    5: "TLB-REFILL",
    6: "NOT-TAKEN",
    7: "MISPRED",
    8: "LLC-ACCESS",
    9: "LLC-REFILL",
    10: "REMOTE-ACCESS",
    11: "ALIGNMENT",
    17: "SVE-PARTIAL-PRED",
    18: "SVE-EMPTY-PRED",
}
KNOWN_EVENTS = sum(1 << bit for bit in EVENT_NAMES)


@dataclass(frozen=True)
class Chunk:
    offset: int
    file_offset: int
    size: int
    reference: int
    cpu: int
    index: int
    tid: int
    conversion: dict[str, int] | None


@dataclass(frozen=True)
class Packet:
    kind: str
    index: int
    value: int
    width: int
    raw: bytes
    offset: int
    file_offset: int


@dataclass
class PerfData:
    data: bytes
    streams: dict[tuple[int, int, int], list[Chunk]]
    audit: dict[str, Any]


def read_perf(
    path: Path, expected_cpu: int | None = None, *, allow_empty: bool = False
) -> PerfData:
    """Validate a little-endian perf v2 file and index its AUXTRACE records."""
    data = path.read_bytes()
    if len(data) < 72 or data[:8] != b"PERFILE2":
        raise ValueError(f"Expected little-endian perf v2 file: {path.name}")
    position, size = struct.unpack_from("<QQ", data, 40)
    end = position + size
    if position < 72 or end > len(data):
        raise ValueError("Invalid perf data section extent")
    types: Counter[int] = Counter()
    streams: dict[tuple[int, int, int], list[Chunk]] = {}
    conversions: list[dict[str, int]] = []
    aux_metadata: list[dict[str, str]] = []
    auxtrace_info_types = []
    conversion = None
    while position < end:
        if position + 8 > end:
            raise ValueError("Truncated perf event header")
        kind, _, length = struct.unpack_from("<IHH", data, position)
        if length < 8 or position + length > end:
            raise ValueError(f"Invalid perf event size at {position}")
        types[kind] += 1
        following = position + length
        if kind in (2, 13, 72):
            raise ValueError(f"Loss/error perf record type {kind} at {position}")
        if kind == 79:
            if length < 32:
                raise ValueError("Truncated TIME_CONV record")
            shift, multiplier, zero = struct.unpack_from("<QQQ", data, position + 8)
            conversion = {
                "shift": shift,
                "multiplier": multiplier,
                "zero": zero,
                "cycles": 0,
                "mask": MASK64,
                "cap_zero": 1,
                "cap_short": 0,
            }
            if length >= 50:
                cycles, mask, cap_zero, cap_short = struct.unpack_from(
                    "<QQBB", data, position + 32
                )
                conversion.update(
                    cycles=cycles, mask=mask, cap_zero=cap_zero, cap_short=cap_short
                )
            if shift > 63 or not multiplier or conversion["cap_zero"] not in (0, 1):
                raise ValueError("Unsupported TIME_CONV values")
            conversions.append(conversion)
        elif kind == 11:
            if length < 32:
                raise ValueError("Truncated PERF_RECORD_AUX")
            offset, count, flags = struct.unpack_from("<QQQ", data, position + 8)
            aux_metadata.append(
                {"offset": str(offset), "size": str(count), "flags": hex(flags)}
            )
            if flags & 0xFF:
                raise ValueError(f"AUX loss/partial/collision flags {flags:#x}")
        elif kind == 70:
            if length < 16:
                raise ValueError("Truncated AUXTRACE_INFO")
            trace_type, reserved = struct.unpack_from("<II", data, position + 8)
            if trace_type != 4 or reserved:
                raise ValueError("AUXTRACE_INFO is not supported ARM SPE type 4")
            auxtrace_info_types.append(trace_type)
        elif kind == 71:
            if length != 48:
                raise ValueError("Unsupported AUXTRACE header size")
            count, offset, reference, index, tid, cpu, reserved = struct.unpack_from(
                "<QQQIIII", data, position + 8
            )
            if reserved or following + count > end:
                raise ValueError("Invalid AUXTRACE payload extent/reserved field")
            if expected_cpu is not None and cpu != expected_cpu:
                raise ValueError(
                    f"AUXTRACE CPU {cpu} disagrees with expected CPU {expected_cpu}"
                )
            key = (cpu, index, tid)
            streams.setdefault(key, []).append(
                Chunk(offset, following, count, reference, cpu, index, tid, conversion)
            )
            following += count
        position = following
    if not streams and (
        (expected_cpu is None and not allow_empty) or not auxtrace_info_types
    ):
        raise ValueError(
            "Empty AUX capture requires declared CPU and ARM SPE AUXTRACE_INFO"
        )
    return PerfData(
        data,
        streams,
        {
            "event_types": dict(types),
            "aux_metadata": aux_metadata,
            "time_conversions": [
                {key: str(value) for key, value in item.items()} for item in conversions
            ],
            "stream_count": len(streams),
            "auxtrace_info_types": auxtrace_info_types,
            "declared_cpu": expected_cpu,
        },
    )


def stream_bytes(
    perf: PerfData, chunks: list[Chunk]
) -> tuple[bytes, list[dict[str, Any]]]:
    """Join only one contiguous stream; permit documented perf alignment padding."""
    payload = bytearray()
    segments = []
    expected = 0
    for chunk in chunks:
        delta = chunk.offset - expected
        trimmed = 0
        if delta:
            # perf AUXTRACE payloads are file-aligned; at most seven trailing zero
            # bytes may lie beyond the producer's next AUX offset.
            if not segments or delta > 0 or delta < -7 or any(payload[delta:]):
                raise ValueError(f"Unexplained AUX stream gap/overlap: {delta}")
            trimmed = -delta
            del payload[delta:]
            segments[-1]["end"] -= trimmed
        start = len(payload)
        payload.extend(perf.data[chunk.file_offset : chunk.file_offset + chunk.size])
        segments.append(
            {
                "start": start,
                "end": len(payload),
                "chunk": chunk,
                "previous_alignment_bytes_removed": trimmed,
            }
        )
        expected = chunk.offset + chunk.size
    return bytes(payload), segments


def read_packet(
    data: bytes, position: int, aux_offset: int, file_offset: int
) -> Packet:
    header = data[position]
    if header in (0, 1):
        return Packet(
            "pad" if header == 0 else "end",
            0,
            0,
            0,
            data[position : position + 1],
            aux_offset,
            file_offset,
        )
    extended = header & 0xFC == 0x20
    if extended and position + 1 >= len(data):
        raise ValueError("Truncated extended SPE packet header")
    actual = data[position + 1] if extended else header
    if extended and actual == 0:
        alignment = 1 << ((header & 15) + 1)
        length = alignment - aux_offset % alignment
        if position + length > len(data):
            raise ValueError("Truncated SPE alignment packet")
        return Packet(
            "alignment",
            0,
            0,
            0,
            data[position : position + length],
            aux_offset,
            file_offset,
        )
    width = 1 << ((actual >> 4) & 3)
    length = 1 + int(extended) + width
    if position + length > len(data):
        raise ValueError(f"Truncated SPE packet at AUX offset {aux_offset}")
    index = (((header & 3) << 3) | (actual & 7)) if extended else actual & 7
    if actual & 0xF8 == 0xB0:
        kind = "address"
    elif actual & 0xF8 == 0x98:
        kind = "counter"
    elif header == 0x71:
        kind = "timestamp"
    elif header & 0xCF == 0x42:
        kind = "event"
    elif header & 0xCF == 0x43:
        kind = "source"
    elif header & 0xFC == 0x64:
        kind, index = "context", header & 3
    elif header & 0xFC == 0x48:
        kind, index = "operation", header & 3
    else:
        raise ValueError(f"Unknown SPE header {header:#x} at AUX offset {aux_offset}")
    value = int.from_bytes(
        data[position + 1 + int(extended) : position + length], "little"
    )
    return Packet(
        kind,
        index,
        value,
        width * 8,
        data[position : position + length],
        aux_offset,
        file_offset,
    )


def operation_text(category: int, value: int) -> str:
    if category != 1:
        return f"OP-TYPE class={category} payload={value:#x}"
    names = ["ST" if value & 1 else "LD"]
    if value & 0xE2 == 2:
        names.extend(
            name
            for bit, name in ((2, "AT"), (3, "EXCL"), (4, "AR"))
            if value & (1 << bit)
        )
    elif value & 0x0A == 8:
        names.extend(("EVLEN", str(32 << ((value >> 4) & 7))))
        if value & 4:
            names.append("PRED")
        if value & 128:
            names.append("SG")
    else:
        names.append(
            {
                0: "GP-REG",
                4: "SIMD-FP",
                16: "UNSPEC-REG",
                48: "NV-SYSREG",
                20: "MTE-TAG",
            }.get(value & 0xFE, "UNKNOWN")
        )
    return " ".join(names)


def time_ns(ticks: int, conversion: dict[str, int] | None) -> str | None:
    if conversion is None or not conversion["cap_zero"]:
        return None
    if conversion["cap_short"]:
        ticks = conversion["cycles"] + (
            (ticks - conversion["cycles"]) & conversion["mask"]
        )
    return str(
        (
            conversion["zero"]
            + ((ticks * conversion["multiplier"]) >> conversion["shift"])
        )
        & MASK64
    )


def decode_stream(
    perf: PerfData,
    chunks: list[Chunk],
    file_id: str,
    audit: Counter[str],
    materialize: Callable[[dict[str, Any]], bool] | None = None,
) -> Iterator[dict[str, Any]]:
    """Validate all records; optionally omit output details for rejected contexts."""
    payload, segments = stream_bytes(perf, chunks)
    audit["alignment_padding_bytes"] += sum(
        s["previous_alignment_bytes_removed"] for s in segments
    )
    current: list[Packet] = []
    position = segment_index = ordinal = 0
    chunk = chunks[0]
    stream_id = f"{file_id}:{chunk.cpu}:{chunk.index}:{chunk.tid}"
    while position < len(payload):
        while position >= segments[segment_index]["end"]:
            segment_index += 1
        segment = segments[segment_index]
        chunk = segment["chunk"]
        local = position - segment["start"]
        packet = read_packet(
            payload, position, chunk.offset + local, chunk.file_offset + local
        )
        audit[f"packet_{packet.kind}"] += 1
        if packet.kind == "address" and packet.index == 0:
            if current:
                raise ValueError(f"Unterminated SPE record in {stream_id}")
            ordinal += 1
        elif not current and packet.kind not in ("pad", "alignment"):
            raise ValueError(f"Orphan SPE {packet.kind} packet in {stream_id}")
        if current or (packet.kind == "address" and packet.index == 0):
            current.append(packet)
            if packet.kind in ("timestamp", "end"):
                row = normalize_packets(current, materialize)
                if "raw_packets" in row:
                    row.update(
                        record_id=f"{stream_id}:{ordinal}",
                        file=file_id,
                        stream_id=stream_id,
                        cpu=chunk.cpu,
                        aux_tid=chunk.tid,
                        aux_index=chunk.index,
                        aux_reference=str(chunk.reference),
                    )
                    row["time_ns"] = (
                        time_ns(int(row["ticks"]), chunk.conversion)
                        if row["ticks"]
                        else None
                    )
                audit["records"] += 1
                yield row
                current = []
        position += len(packet.raw)
    if current:
        raise ValueError(f"Unterminated final SPE record in {stream_id}")


def normalize_packets(
    packets: list[Packet],
    materialize: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Check every field before choosing whether to construct the full output."""
    context: dict[str, Any] = {"ticks": None, "tid": None}
    seen = set()
    for packet in packets:
        kind, index = packet.kind, packet.index
        if kind not in ("pad", "alignment", "end"):
            key = (kind, index if kind in ("address", "counter") else 0)
            if key in seen:
                raise ValueError(f"Duplicate SPE field {key}")
            seen.add(key)
        if kind == "timestamp":
            context["ticks"] = str(packet.value)
        elif kind == "context":
            context["tid"] = packet.value
    if materialize is not None and not materialize(context):
        return context
    row: dict[str, Any] = {
        field: None
        for field in (
            "pc",
            "va",
            "pa",
            "pc_payload",
            "va_payload",
            "pa_payload",
            "ticks",
            "context_id",
            "context_el",
            "tid",
            "op_class",
            "op_value",
            "operation",
            "event_bits",
            "event_width",
            "data_source",
            "unknown_event_bits",
        )
    }
    row.update(
        counters={},
        other_addresses={},
        event_set_bits=[],
        cache_location="unknown",
        memory_policy="unknown",
    )
    for packet in packets:
        kind, index, value = packet.kind, packet.index, packet.value
        if kind == "address":
            field = {0: "pc", 2: "va", 3: "pa"}.get(index)
            if field:
                row[field] = hex(value & MASK56)
                row[field + "_payload"] = f"0x{value:016x}"
                if index == 0:
                    row.update(pc_el=(value >> 61) & 3, pc_ns=value >> 63)
                elif index == 3:
                    row.update(
                        pa_ns=value >> 63,
                        pa_ch=(value >> 62) & 1,
                        pa_pat=(value >> 56) & 15,
                    )
            else:
                row["other_addresses"][str(index)] = hex(value)
        elif kind == "counter":
            row["counters"][str(index)] = value
        elif kind == "timestamp":
            row["ticks"] = str(value)
        elif kind == "context":
            row.update(context_id=hex(value), context_el=index + 1, tid=value)
        elif kind == "event":
            row.update(
                event_bits=f"0x{value:08x}",
                event_width=packet.width,
                event_set_bits=[
                    bit for bit in range(packet.width) if value & (1 << bit)
                ],
                unknown_event_bits=f"0x{value & ~KNOWN_EVENTS:08x}",
            )
        elif kind == "operation":
            row.update(
                op_class=index,
                op_value=hex(value),
                operation=operation_text(index, value),
            )
        elif kind == "source":
            row["data_source"] = hex(value)
    row["counter_presence"] = {
        str(index): str(index) in row["counters"] for index in range(32)
    }
    for index, field in ((0, "tot"), (1, "issue"), (2, "xlat"), (6, "counter6")):
        row[field] = row["counters"].get(str(index))
    row["raw_hex"] = b"".join(packet.raw for packet in packets).hex()
    row["raw_packets"] = [
        {
            "kind": p.kind,
            "index": p.index,
            "value": hex(p.value),
            "width": p.width,
            "offset": str(p.offset),
            "file_offset": str(p.file_offset),
            "hex": p.raw.hex(),
        }
        for p in packets
    ]
    return row
