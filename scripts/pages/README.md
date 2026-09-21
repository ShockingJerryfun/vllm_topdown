# Isolated executable-file page experiment

This directory supplies preparation and evidence tools, not a completed 4KB/64KB measurement. The kernel base page stays 4096 bytes. The `64k` condition requests executable-file compound folios using the historical `numa_locality/exec_64k/exec_prefault.c` mechanism. Physical contiguity, compound folios, PTE encoding and the base page size remain separate claims.

## Source and isolation

- `prepare.py` selects every ordinary executable ELF in the supplied live maps and/or explicit path list, including the Python executable, loader/libc, CUDA driver/runtime, PyTorch/vLLM extensions and generated Triton launchers. The exact selected paths, executable PT_LOAD ranges, SHA256 and source/copy inode identities are recorded in `identity.json`. Historical `cache_reload/dependencies.txt` is only a candidate discovery list; current EngineCore/API maps determine missing or changed files.
- Copies contain identical bytes but have separate inodes. `namespace_exec` creates a new recursively private mount namespace and binds copies read-only over their original paths. The launched service and children retain original paths, RPATH and absolute-dlopen behavior. Existing container/host processes retain their original mount view. This requires Linux CAP_SYS_ADMIN; no running code mapping is replaced.
- `cold.py` advises only frozen task-copy inodes. Invoke it before any process uses those copies, and after confirming prior task users have exited. It never calls global `drop_caches`, never advises original dependencies and never alters file contents.
- `prefault` uses only newly allocated helper VMAs. Its MAP_FIXED replaces an address range reserved by that same helper. It does not attach to, write into, or remap the service address space.
- No tool writes sysfs, changes kernel modules or changes automatic NUMA balancing. `64k` fails closed if the existing executable-mTHP BIT1 is off.

The kernel source patch implementing this control has a `file_exec_mthp_enabled()` guard for the 64KB path. Explicit `MADV_HUGEPAGE` instead enters the PMD-order path; it does not substitute for the 64KB switch. The running kernel's exact source/build must still be matched: [openEuler patch 05/12](https://mailweb.openeuler.org/archives/list/kernel@openeuler.org/thread/NEWBFLIWKVHG2GRUI2VCSE72LAVQL4M5/). A temporary global switch affects concurrent executable faults outside the task; restoring the switch does not undo folios allocated by other users during that interval.

## Invocation

Compile statically on the target Linux host, then make the binaries available inside the original container:

```sh
cc -O2 -Wall -Wextra -Werror -static namespace_exec.c -o namespace_exec
cc -O2 -Wall -Wextra -Werror -static prefault.c -o prefault
cc -O2 -Wall -Wextra -Werror -static process_pages.c -o process_pages
```

Example variables below must be set from the current task's verified node/CPU assignment. `condition` is a new task directory, `page_tools` is this directory in the container, `maps_file` is a fresh saved service map. Run preparation in the original container, where `/` resolves original library paths correctly. Hashing deliberately precedes cache eviction. The preparation process should already have CPU-local memory policy, since copying touches all file bytes.

```sh
python3 "$page_tools/prepare.py" --source-root / --maps "$maps_file" --output "$condition"
python3 "$page_tools/cold.py" "$condition"
"$page_tools/prefault" "$mode" "$prefault_cpu" "$cpu_node" \
    "$condition/segments.tsv" "$condition/files" "$condition/prefault.tsv" 1
numactl --physcpubind="$launch_cpu" --membind="$cpu_node" \
    "$page_tools/namespace_exec" "$condition/paths.txt" "$condition/files" COMMAND ARGUMENTS
```

Use `--paths /path/to/dependencies.txt` to supply a verified frozen executable-file list, optionally together with repeated `--maps`. Do not run Python, a hash scan, a library inventory or other code that reads copy files between `cold.py` and `prefault`. `require-cold=1` refuses cached code ranges. Multiple overlapping ELF executable ranges may legitimately overlap in cache; inspect that manifest explicitly before deciding whether a subsequent prefault with `0` is appropriate. Never treat prefault return success as proof of resulting folio sizes.

The CPU/node binding must precede `namespace_exec`, so even the launched program's dynamic loader faults copied data under CPU-local policy. Do not place a dynamically linked binding utility inside the private namespace before policy is installed. `namespace_exec` emits its new mount namespace and file count. Verify that its namespace differs from the original container namespace and that originals still resolve to their original frozen inodes outside the private namespace. The service may now hold copied pages; stop only the task's service, then allow its namespace to disappear. There are no mounts in the original view to unmount.

## Prevent later code-page collapse

`code_guard.c` is an opt-in glibc `LD_AUDIT` library. It applies `MADV_NOHUGEPAGE` only to ordinary file-backed executable VMAs: executable permission, nonzero inode and an absolute pathname outside `/dev/`. It scans all current mappings, including the guard and all loader namespaces. Anonymous executable mappings retain their separate strict acceptance rules. Non-executable data VMAs, process-wide THP policy and shared kernel settings are untouched.

The guard scans at its constructor, `la_version`, `la_preinit` and `la_activity(LA_ACT_CONSISTENT)`. glibc issues the consistent notification before constructors of newly loaded objects in [`dl-open.c`](https://github.com/bminor/glibc/blob/glibc-2.39/elf/dl-open.c); the native probe below checks that ordering for the actual runtime. `la_objopen` returns zero and there are no symbol-binding or PLT-call callbacks. This avoids wrapping `dlopen` or changing its caller-dependent library search. `LD_AUDIT` still adds a loader module and loader notifications, so use the identical guard binary in every compared condition and warm dynamic loading before measurement.

The library uses raw Linux system calls, fixed buffers and no libc dependency, allocation, loader calls, threads or background work. It finishes reading `/proc/self/maps` before applying advice. Malformed/truncated maps, more than 8192 selected executable mappings, a line of 16384 bytes or more, callback reentry or a failed system call terminate the process with exit 126. Concurrent direct unmapping or permission changes outside the loader can therefore cause a closed failure. There is no continuous monitor for direct executable `mmap`/`mprotect` operations; the endpoint verifier rejects any ordinary executable mapping left without `nh`. Static executables and secure-execution cases that ignore `LD_AUDIT` are outside this mechanism; declared conditions require the guard's actual mapping.

Build on Linux AArch64; omit `-mno-outline-atomics` on x86_64:

```sh
cc -O2 -Wall -Wextra -Werror -shared -fPIC -fvisibility=hidden \
    -fno-builtin -fno-stack-protector -mno-outline-atomics -nostdlib \
    -Wl,-z,defs code_guard.c -o code_guard.so
cc -O2 -Wall -Wextra -Werror guard_probe.c -ldl -o guard_probe
cc -O2 -Wall -Wextra -Werror -DGUARD_PROBE_DSO -shared -fPIC \
    guard_probe.c -o guard_probe.so
LD_AUDIT="$PWD/code_guard.so" ./guard_probe "$PWD/guard_probe.so" 0
```

Check `readelf -d`, `readelf -Ws` and the build hash: the guard must have no `DT_NEEDED`, no undefined external dependencies and only the four audit entry points exported. The probe checks `nh` in constructors, main, `dlopen`, `dlmopen`, `dlclose`, fork/exec children and after an optional hold of up to 3600 seconds. It also checks that non-executable mappings have no `nh` and `PR_GET_THP_DISABLE` is zero. This is a flag/timing probe, **not physical-folio evidence**. Use the page snapshots and actual long-running service to test physical stability for at least the interval that previously produced collapse.

Add the canonical guard ELF to preparation before cold/prefault:

```sh
python3 "$page_tools/prepare.py" --source-root / --maps "$maps_file" \
    --code-guard "$guard_path" --output "$condition"
```

The guard is an ordinary independent copy in `files`, `paths.txt` and `segments.tsv`, with the same cold/prefault and NUMA treatment as other executable files. `identity.json` adds exactly `code_page_guard: {path, sha256, method}`, where the method is `executable_vma_madv_nohugepage`. The path is the original canonical path at which the condition copy will be mounted; each condition keeps its own copy inode. Set `CODE_PAGE_GUARD` to that path for `namespace_exec`, which installs `LD_AUDIT` **after** binding the condition files. Do not export the audit library before the private mounts. All long-lived task processes using the copies must inherit the same guard; an unguarded process mapping the same file inode can leave a route to file-cache collapse.

When the descriptor is present, `verify.py` requires its SHA/path to match an ordinary frozen file record, an actual executable guard mapping, and exact raw-smaps `VmFlags` evidence containing `nh` without `hg` for every ordinary file executable VMA. `phases.before/after.code_page_guard` reports `declared`, `loaded`, descriptor fields, `ordinary_file_executable_mappings` and `nh_mappings`. Missing or malformed declarations and evidence fail; identities without the descriptor retain the historical qualification contract.

`MADV_NOHUGEPAGE` marks future VMA eligibility; it does not establish actual 4KB backing or repair already collapsed 2MB file folios. Cold independent copies and actual page verification remain required. Preservation and mapping of pre-existing exact64KB folios under `nh` require native qualification on the running kernel; neither the short probe nor a policy label proves this. Guard advice alone never replaces the unchanged physical 4KB/exact64KB, NUMA, inode and before/after stability checks.

## Runtime gates

On the host, at quiescent ready/before/after gates for each EngineCore process:

```sh
python3 "$page_tools/snapshot.py" --pid "$host_pid" --node "$cpu_node" \
    --identity "$condition/identity.json" --output "$evidence/PHASE" \
    --original-namespace "$original_container_mount_namespace" \
    --query "$page_tools/process_pages"
```

This requires host permissions for `/proc/PID/pagemap`, `/proc/kpageflags`, `/proc/kpagecount` and cross-process `move_pages` queries. `move_pages` is called with a null node array: it queries location and does not migrate. `process_pages.c` extends the historical read-only helper to query `/dev/shm` and `/dev/zero` RAM; it reports N0..N3, and other nodes require an explicit extension. Recompile it after updating this directory. Both Python tools import the sibling `evidence.py`, which must be deployed with them.

The original namespace reference must come from the verified original container init process's `/proc/PID/ns/mnt` link, such as `mnt:[4026533000]`. Supply `--original-namespace` or `CODE_PARENT_MOUNT_NAMESPACE`; verification compares the Worker namespace with that original reference. The snapshot observer can legitimately run inside the Worker's private namespace. An absent original reference fails acceptance; the observer namespace is not used as a substitute.

`anonymous_exec.json` describes every ordinary inode-zero anonymous/heap VMA with executable permission, including its address range, permissions, resident/nonresident counts, backing and NUMA counts. For origin investigation, at most 64KB per VMA and 1MB total of **already resident** anonymous executable pages are read from `/proc/PID/mem`, with PFN/presence checks before and after each read. Captured bytes live in `anonymous_exec/` with SHA256 and addresses recorded in the JSON. Ordinary data, file-backed executable mappings and kernel special mappings are not dumped. Byte limits, permission/read failures and unstable pages remain explicit unknowns. This read-only diagnostic can touch already resident code, so it belongs at the quiescent gate. Anonymous executable mappings fail unless they satisfy the narrow runtime closure rule below; observed bytes do not prove execution history.

`closure.py` recognizes only the observed AArch64 libffi/ctypes layout, supported by the saved `evidence/anonymous_probe` page, maps, target ELF binaries and disassembly. Its instruction template and parameter positions match [libffi 3.4.4 `ffi_prep_closure_loc`](https://github.com/libffi/libffi/blob/v3.4.4/src/aarch64/ffi.c#L795-L869); the top-chunk metadata matches [dlmalloc `init_top`](https://github.com/libffi/libffi/blob/v3.4.4/src/dlmalloc.c#L3016-L3027). The exception permits at most one unnamed inode-zero private RWX mapping of exactly 4096 bytes. Every contiguous 56-byte allocation must contain the exact 16-byte trampoline, valid handler/callback pointers, and the observed ctypes CIF/user-data relationship. Remaining bytes must match the allocator top-chunk/72-byte footer structure and zero padding. This is deliberately narrower than all possible libffi allocator states: freed chunks, changed layouts, extra code and additional anonymous executable pages fail.

For this inspected build, handler pointers must resolve through the **current round's actual executable mappings** to `/usr/lib64/libffi.so.8.1.2` at file offset `0x6800`; callbacks must resolve to `/usr/local/lib/python3.13/lib-dynload/_ctypes.cpython-313-aarch64-linux-gnu.so` at `0x161f8`. Both mappings must match independent copy device/inode and SHA256 identities frozen for the current condition. The only supported ELF builds are libffi SHA256 `3343107f68508d8668e559a0f0f8bec56d2a31d47ec60882a12700f96423139c` and ctypes SHA256 `ad763c8ac89a717d9a7b82702d20c2d6bb782238827ee1f976a50d3c3f92ddcc`, verified against the saved target ELF bytes and disassembly. A different build fails even with the same full path, inode and target offset. No cross-process virtual address, anonymous-page content hash, or basename allowlist is used. Those build-specific paths/offsets and verified ELF hashes are a compatibility boundary: a new build requires fresh evidence and an explicit classifier update.

The closure must have real noncompound 4KB backing and a stable local PFN. Its full captured content SHA256, PFN, node and target-library identities must match before/after. `phases.*.runtime_closure_exceptions` records the classification and exact targets; `counts.code_runtime_closure_pages` and `stability.runtime_closure_changed` make the exception auditable. This page remains in total code and 4KB counts and in the 64KB coverage denominator; it never increases exact64KB coverage. Ordinary executable ELF rules remain unchanged. Deploy `closure.py` alongside `verify.py` and `evidence.py`.

At the quiescent gates immediately before and after the formal requests, capture the same Worker PID and exact condition identity. Then run the offline acceptance tool:

```sh
python3 "$page_tools/verify.py" --before "$round/pages/before" \
    --after "$round/pages/after" --identity "$identity" --mode "$mode" \
    --node "$cpu_node" --output "$round/pages/verification.json"
```

The tool exits 0 only for `status=pass`; rejected or incomplete evidence yields `status=fail` and exit 1. Its schema-1 JSON binds both endpoint paths and evidence hashes, Worker PID/start ticks/mount namespace, exact identity-manifest hash, mode and node. Coverage fractions always retain their numerator and denominator. The snapshot's schema-2 `page_evidence.csv` preserves each resident page's flags, mapcount, pagemap bits, actual NUMA query and PFN recheck. File mappings containing COW pages are classified per page; a pathname alone never establishes file-cache backing. Raw mapcount counts mappings, not distinct processes.

For an async-scheduling comparison where page size is a recorded covariate, set `CODE_PAGE_COVERAGE_POLICY=observe` in both configurations (CLI `--coverage-policy observe`). Only the 99% coverage threshold becomes non-blocking; actual fractions and threshold observations remain in the output. Identity, NUMA and folio validity checks remain active. Set `CODE_PAGE_RESIDENCY_POLICY=observe` (CLI `--residency-policy observe`) to record new/lost resident pages and changes of physical backing without requiring identical resident PFNs at both endpoints. Process, mapping identity and per-endpoint NUMA checks still apply. Formal page-size experiments retain the default `strict` policy.

Acceptance must include all of the following:

1. Actual loaded executable file inodes match task copies; report new/unregistered executable files and anonymous executable maps. Same source and copy hashes before preparation; no source changes afterward. Capture actual imports, loaded DSOs and generated-launcher identity separately.
2. All ordinary observed code pages have stable PFNs during each snapshot and the expected CPU-local node. Compare `code_pages.csv` by path + file offset across before/after snapshots to verify no PFN/node/folio drift during the measurement.
3. `4k`: every observed code page has noncompound backing. `64k`: at least 99% of all resident code-page observations must have exact64KB physical backing. Smaller folios retain their actual sizes in `code_folio_<size>k_pages`, remain in the coverage denominator, and never count as 64KB. Larger folios, including 2MB, fail. The saved compound descriptor checks the aligned physical head and complete tail extent, including the first following non-tail page. Complete file-offset-aligned groups retain the 16-present-page checks in `groups_64k.csv`. Partially resident groups may pass when every observed exact64KB page resolves to the same aligned folio at the matching file offset; they are separately counted in `code_partial_exact_64k_groups`, never as fully resident groups. At least one complete exact64KB group is still required. This proves observed physical backing, not page-table encoding or the residency/location of absent pages.
4. Keep `smaps` KernelPageSize/MMUPageSize/FilePmdMapped and raw `maps`/`numa_maps`. A 4KB smaps field alone neither proves nor disproves 64KB compound backing. CONT PTE bits are not observed by these tools.
5. Ordinary resident CPU-side data stays CPU-local in both near/far conditions, established by raw per-page NUMA queries. Per-page backing counts distinguish file cache, COW, anonymous memory, shared RAM, KSM and confirmed kernel zero pages. Confirmed KPF_ZERO_PAGE pages are excluded from the locality denominator and never counted local; other unknown backing/location fails. Device, kernel-special and inaccessible mappings retain explicit exclusions and unknown residency/location, so a pass cannot establish device-mapping host-RAM locality. This helper prefaults only executable file ranges; other file data is first-touched by the bound runtime. Boundary folios can include adjacent non-code bytes and remain reported.
6. Verify all worker/auxiliary/client threads, per-thread memory policy, actual measurement TIDs and CPU placement using the experiment's binding verifier. These tools do not replace that verifier.

Each file-copy condition should stay alive for its intended sequential rounds; avoid new cold copies between repeats unless the experiment definition requires it. Hash scans after a completed run can warm copy pages, so perform them after all measured rounds.

An executable inode match is insufficient for an anonymous/COW code page, which fails frozen-file acceptance. A task-generated diagnostic ELF can carry `task_generated_diagnostic=true` and its actual `path`, `copy_device`, `copy_inode` and `sha256` in the per-run identity; it need not have a distinct source inode, but all actual page checks still apply. Use this exact extended identity for both endpoint snapshots and verification. A 4KB diagnostic code mapping is reported as a structural small-mapping exception in a 64KB condition.

Strict endpoint stability rejects new, lost or changed resident code pages. Legal first use is quantified as `newly_resident_code_pages` and `needs_warmed_repeat=true`, requiring another warmed round; it is not silently omitted. Data COW/PFN changes are counted but permitted when both endpoints establish valid local pages. Endpoint checks do not prove continuous in-window stability, unresident page placement, current source/copy bytes, or worker-thread binding; retain the separate runtime identity and binding verifiers.

The resident 920B eight-stage profile checks its complete time/PMU cohort at the first `time` boundary and final `imix2` boundary, using `RUN_ROOT/pages`. Each group points to that shared evidence. The independent Host end-to-end cohort retains its own first/last snapshots; hotspot and standalone/SPE captures retain their own endpoints. If a VMA or ordinary code PFN changes during a single quiescent snapshot, the incomplete directory is preserved and the entire snapshot is retried, at most three attempts. This does not waive endpoint page-size, NUMA, identity, or before/after stability checks.

## Local validation

`vllm_fj/.venv/bin/python -m pytest -q work/topdown_binding/pages/test_pages.py work/topdown_binding/pages/test_verify.py work/topdown_binding/pages/test_closure.py work/topdown_binding/pages/test_guard.py` checks preparation plus genuine 4KB/exact64KB acceptance, 2MB rejection, ASLR, COW/shared/zero/device classification, missing evidence, source-inode substitution, PFN drift, newly resident code, identity mismatch and CLI failure. Closure checks include the real probe layout and binary hashes, plus altered instructions, pointer targets, target identity, tail bytes, content drift, unknown code, page-count and locality failures. Guard tests reject descriptor substitution, a missing loaded guard, missing/mismatched `nh` evidence, and physical folios incompatible with the requested mode despite valid `nh`; they also check ordinary independent-copy preparation and backwards compatibility. Linux C compilation, private mount behavior, actual 4KB/64KB folios and GPU/model behavior require target-host verification and are not implied by those local tests.
