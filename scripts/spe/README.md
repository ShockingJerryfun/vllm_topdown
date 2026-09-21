# SPE offline contract

Run with the existing Python environment, either as a module from the task root or
as `python scripts/spe/decode.py --run-dir RUN/spe` after bundling these sibling
modules. The decoder never accesses SSH, GPUs, `/proc`, or live device registers.

The fast offline path validates all AUX packets in a native scan, then materializes
only selected records with the reference Python decoder. Build the helper on the
machine doing the decoding and pass it explicitly:

```sh
cc -O3 -std=c11 -Wall -Wextra -Werror -shared -fPIC fast_scan.c -o fast_scan.so
python decode.py --run-dir RUN/spe --native-library ./fast_scan.so
```

For new standalone captures, `capture.py --skip-perf-dumps --raw-retention selected`
avoids expanding the full binary into packet/memory text. After normalization,
`compact.py --run RUN/spe --discard-raw` replays selected raw packets and checks
their normalized values, windows, and summary counts before deleting that run's
temporary full perf input. Without the explicit capture retention option, deletion
is rejected. Failed processing preserves the raw file. Historical captures are not
pruned by this workflow.

`samples.jsonl.gz` retains each selected record's original packets, offsets, values,
and normalization. `retention.json` lists the original full-file identities and the
retained files. The decoder manifest describes inputs at processing time; after
compaction it is provenance, not a claim that the full perf file still exists.
Window, address, binary identity, and timing-conversion evidence remain available.
The compact package can be checked with `compact.verify_selected` without perf.data.

This is short full-thread recording followed by selection, not hardware gating at
every function boundary. It adds no new model execution markers.

The current collector starts one reader for the main Worker's host TID:

```sh
perf record --no-inherit -t HOST_TID --sample-cpu \
  -e arm_spe_0/load_filter=1,store_filter=1,jitter=1,ts_enable=1,pa_enable=1/u \
  -c 1024 -m 64,512 -o RUN/spe/data/perf.data
```

The period defaults to 1024 and can be changed with capture's `--period`. The
reader follows the target thread within its verified Worker affinity. One file
contains separate AUXTRACE CPU/index/TID streams; the collector does not start a
reader per CPU and does not use system-wide `-a` or a `-C` filter. Metadata `cpus`
is the allowed Worker CPU set, not a claim that each CPU produced samples.

`RUN/spe/spe_capture.json` uses schema version 1:

```json
{
  "schema_version": 1,
  "host_tid": 12345,
  "container_tid": 130,
  "cpus": [248, 250, 252, 254],
  "perf_files": [
    {
      "path": "data/perf.data",
      "cpu": null,
      "target_tid": 12345,
      "reader": {
        "pid": 23456,
        "start_time": "123456789",
        "session_id": 23450,
        "alive_before_stop": true,
        "completed": true,
        "returncode": 0
      },
      "packets_text": "data/perf.packets.txt.gz",
      "memory_text": "data/perf.memory.txt.gz",
      "memory_format": "perf_memory_cpu"
    }
  ],
  "windows": "data/windows.json",
  "maps_file": "evidence/before/maps",
  "binaries": [
    {"mapped_path": "/usr/lib/libexample.so", "path": "evidence/binaries/libexample.so",
     "sha256": "expected snapshot SHA256"}
  ],
  "tools": {"objdump": "/usr/bin/objdump"},
  "physical_numa": "evidence/physical_numa.json",
  "pci_resources": "evidence/pci_resources.json",
  "iomem": "evidence/iomem.txt",
  "gpu_bdf": "0000:ab:00.0"
}
```

Paths are run-directory-relative or absolute. `container_tid` defaults to
`host_tid`; supply the explicit namespace mapping for container markers. Marker
`tid` must equal one of those IDs. The sample context itself must equal
`host_tid`. An explicit `cpu: null` selects the current thread-scoped contract:
there must be exactly one perf file, its `target_tid` must equal `host_tid`, every
AUXTRACE header TID must equal that target, and every AUXTRACE CPU must be in
`cpus`. AUXTRACE TID is also retained as provenance and never substitutes for a
missing sample context. Both CONTEXT EL1 and EL2 values are retained. CONTEXT EL
is not the sampled PC execution EL.

Thread-scoped input always requires an actual ARM SPE `AUXTRACE_INFO` record and
a completed `reader`: positive `pid`, decimal `start_time`,
`alive_before_stop: true`, `completed: true`, and successful flush `returncode`
(`0`, `130`, or `-2`). The completion file named by metadata `capture_complete`
(default `capture_complete.json`) must contain `perf_readers_complete: true`.
The collector also atomically saves startup PID/start/session identities in
`evidence/readers.json` for cleanup; `session_id` is used by the supervisor's
identity checks and is not an additional decoder completion requirement.

With these proofs, some allowed CPUs may have no AUXTRACE stream. Audit records
`recording_scope: "thread"`, `declared_cpus` (the allowed set),
`observed_aux_cpus` (actual stream CPUs), and `empty_aux_cpus` (the difference).
An absent stream does not establish that the CPU never ran the thread. These
lists do not assert formal-window sample coverage on each CPU. The whole run
still requires nonzero selected main-thread samples. Empty independent dumps
are `not_applicable`, with zero checked bytes/samples.

Historical input remains supported: an integer file `cpu` must match all its
AUXTRACE headers; an entirely empty per-CPU file additionally needs ARM SPE
`AUXTRACE_INFO`, completed reader identity, and overall reader completion. A
legacy file without a `cpu` key can contain multiple CPU streams but cannot
claim unobserved allowed CPUs. Current capture uses the single-file contract
above.

The windows input is a list, or an object with a `windows` list. Every row has
`request`, `step`, `tid`, `start`, `end`, `replay_start`, `replay_end`; optional
`cpu_start` and `cpu_end` must be in the capture CPU set. Ticks accept decimal
strings. Use the same CNTVCT clock for capture markers and SPE timestamps. Windows
must be nonoverlapping and Replay must be nested. Formal selection is `request >
0`, exact observed host context ID, and `[start, end)`; Replay is labeled using
its own half-open interval. The decoder does not prescribe request or step
counts. The current collector runs one warmup request (`0`) and three formal
requests (`1`–`3`), checking `RANDOM_OUTPUT_LEN - 1` windows per request.

The decoder retains the original CPU/index/TID stream keys. Interleaved streams
are decoded separately; packet splits across contiguous chunks are reassembled.
At most seven trailing zero bytes added by perf's AUXTRACE file alignment can be
removed when the next same-stream AUX offset proves they are padding. Other
gaps/overlaps, unknown packet headers, unfinished records, duplicate record
fields, lost records, AUX errors and low-byte loss/partial/collision flags fail
closed. The raw perf file and dumps remain unchanged.

Every raw record is parsed and checked before the window/TID predicate decides
whether to construct its output details. Excluded records omit `raw_hex`,
`raw_packets`, counter-presence maps, and other full-output fields internally;
their exclusions and every record/packet count are still exact. Selected sample
fields and independent memory matching are unchanged. The low-level
`decode_stream` and `normalize_packets` APIs retain their default full output;
their optional `materialize` callback receives only `ticks` and `tid` after
duplicate-field validation. This optimization does not bypass any raw-stream,
packet-boundary, unknown-header, orphan, or unterminated-record checks.

INFO logs identify raw indexing, each CPU stream, independent checks, PC
resolution, sample enrichment, summaries, and final input hashing. Raw decode
progress is time-throttled to at most once per 30 seconds, checked every 65,536
records; it reports decoded records and selected samples so far.

Output under `analysis/`:

- `samples.jsonl.gz`: one selected sample per row; addresses and ticks are strings;
  CPU/TID/namespace TID, request/step, raw operation/events/data source, all counter
  indices and their independent presence, full record bytes and ordered packet
  bytes/stream offsets/original file offsets are retained.
  Optional address evidence adds `physical_resource`, `ram_numa`, `pci_bdf`,
  exact-text `bar_offset`, and `data_mapping`; missing information remains null
  or explicitly unknown.
- `pc_summary.json`: array keyed by exact snapshot SHA256 and file offset when
  available, or an explicitly `unresolved:`-prefixed identity. CPU/TID/request
  counts and all 32 raw event set counts are retained. Counter distributions have
  separate present/missing counts. Counter 6 remains unnamed raw evidence.
  Address context distributions retain observed resource, RAM node, PCI BDF,
  and VA mapping counts.
- `windows.json`: validated input markers; timestamps rendered as decimal strings.
- `audit.json`: `status`, selected sample and PC counts, input/stream details,
  exclusions, resolution coverage and independent check results.
- `manifest.json`: `status`, schema version, input hashes, file names, scope and
  semantic boundaries. A failed retry invalidates the old success manifest.

ELF file offsets come from the exact maps snapshot. ELF virtual addresses and
function start/end addresses come from the matching saved ELF64 binary; only
nonzero-sized function symbols establish function boundaries. Disassembly is
accepted only when its instruction bytes match the same file bytes. Missing
binaries, symbols or compatible objdump support remain explicit unknowns.
Snapshot identity does not by itself establish the runtime origin of a capture;
the collector must save and attest the matching maps and binaries.

Optional independent checks are mandatory when their paths are supplied:

- `packets_text`: `perf script -D -i FILE` output, optionally gzip-compressed.
  Every dumped AUX byte is matched against the original file's chunks, including
  file padding. Raw event names are not used to infer cache service location.
- `memory_text` with `memory_format: "perf_memory_cpu"`: current collector output
  of `perf script -i FILE --itrace=M -F
  pid,tid,cpu,time,addr,data_src,weight,ip,phys_addr`, accepting perf's
  `pid/tid [CPU] time: ...` layout. Matching includes the independently printed
  CPU, observed TID, PC/VA/PA/TOT, and timestamp microseconds after the actual
  TIME_CONV associated with each AUX chunk. `cpu_checked` is true.
  An alternate `memory_format: "fields"` accepts whitespace-separated
  `pid tid cpu time ip addr phys_addr weight` with the same CPU-inclusive match.
  Historical `memory_format: "perf_memory"` (also the default when omitted)
  accepts the older dump without `cpu` in the `-F` list; its `cpu_checked` is
  false and it does not independently verify CPU identity.

Absent independent dumps are `not_provided`, never `pass`. Samples lacking
addresses or required counters cannot be checked against memory samples but
remain in the raw packet check and selected sample output. Cache service location
and memory policy are `unknown`; no latency-to-cache heuristic is applied.
If a memory dump is supplied, selected samples require usable captured
`TIME_CONV`; a missing conversion cannot produce a vacuous successful match.

Optional PA classification uses only observed bounds: the selected GPU's
assigned memory BAR0–5 resources, then `System RAM` from iomem and a unique
physical memory-block NUMA node. `data_mapping` is the VA's maps-snapshot line.
Missing or redacted resource evidence remains unknown; these fields establish
neither cache service level, memory write policy, nor HBM access.

The current decoder is not a fixed-memory streaming implementation. It reads
each raw perf file into memory and joins one complete AUX stream at a time;
joining temporarily holds both bytearray and bytes copies. It also retains
selected-sample matching keys and selected rows for PC aggregation. Packet and
memory gzip text is read line by line, but all decompressed lines must be
examined, and input hashes are checked before and after processing. Large raw
captures therefore require memory beyond their on-disk size. Text validation
work scales with decompressed text, not just compressed file size. Address
enrichment scans maps/resource intervals per selected sample, and PC summaries
sort present counter values to retain exact median/p95 values.

Checks: `python -m pytest -q work/topdown_binding/tests/test_spe_decode.py`.
The optional historical regression is enabled with
`SPE_HISTORICAL_ROOT=work/spe_location/raw/session` and
`work/topdown_binding/tests/test_spe_historical.py`; it reads that archive without
modifying it and checks 44,076 selected samples / 2,353 PCs plus both independent
perf outputs. These tests do not establish that a new live capture succeeded.
