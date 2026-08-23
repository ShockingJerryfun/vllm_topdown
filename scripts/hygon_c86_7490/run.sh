#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SCRIPT_DIR")
SOURCE_ROOT=$(dirname "$COMMON_DIR")
RUN_ROOT=${RUN_ROOT:-$SOURCE_ROOT/results/hygon_c86_7490}
PYTHON_BIN=${PYTHON_BIN:-$(dirname "${VLLM_BIN:-vllm}")/python}
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN=python3
"$PYTHON_BIN" -c 'import openpyxl' >/dev/null 2>&1 || {
    printf 'Missing openpyxl; install scripts/requirements-report.txt\n' >&2
    exit 6
}
[[ ! -e "$RUN_ROOT" ]] || { printf 'Exists: %s\n' "$RUN_ROOT" >&2; exit 2; }
install -d -m 755 "$RUN_ROOT"

grep -qm1 'vendor_id.*HygonGenuine' /proc/cpuinfo || {
    printf 'This collector requires HygonGenuine CPU\n' >&2
    exit 3
}
AMD_L3_ROOT=/sys/bus/event_source/devices/amd_l3
[[ -r "$AMD_L3_ROOT/type" && -r "$AMD_L3_ROOT/cpumask" ]] || {
    printf 'Missing amd_l3 PMU type or cpumask under %s\n' "$AMD_L3_ROOT" >&2
    exit 3
}

VLLM_SITE=${VLLM_SITE:-$("$PYTHON_BIN" -c 'import importlib.util; print(next(iter(importlib.util.find_spec("vllm").submodule_search_locations)))')}
RUNTIME=$(mktemp -d /tmp/vllm.XXXXXX)
trap 'rm -rf -- "$RUNTIME"' EXIT
cp -rs "$VLLM_SITE" "$RUNTIME/vllm"
while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_ROOT/vllm/"}
    target="$RUNTIME/vllm/$relative"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$file" "$target"
done < <(find "$SOURCE_ROOT/vllm" -type f -print0)
ln -s "$SOURCE_ROOT/kperf_instrument.py" "$RUNTIME/kperf_instrument.py"
export SOURCE_ROOT VLLM_PYTHONPATH=$RUNTIME

{
    lscpu
    printf '\nperf_event_paranoid='
    cat /proc/sys/kernel/perf_event_paranoid
    printf 'nmi_watchdog='
    cat /proc/sys/kernel/nmi_watchdog
} > "$RUN_ROOT/platform.txt"
PYTHONPATH="$VLLM_PYTHONPATH" VLLM_USE_V2_MODEL_RUNNER=1 "$PYTHON_BIN" -c \
    'import os, sys, torch, vllm; from vllm.v1.worker.gpu import model_runner; from vllm.model_executor.models import qwen3; print(sys.version); print(vllm.__version__); print(torch.__version__); print(os.path.realpath(vllm.__file__)); print(os.path.realpath(model_runner.__file__)); print(os.path.realpath(qwen3.__file__))' \
    > "$RUN_ROOT/runtime.txt"

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" time
"$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/time" \
    --mode time \
    --expected-calls "$(( ${RANDOM_OUTPUT_LEN:-100} - 1 ))"

while IFS='|' read -r label codes names; do
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" "$label" "$codes" "$names"
    case "$label" in
        topdown|spec_ls|spec_ase)
            printf 'metric_basis=Hygon Zen1 proxy\n' \
                >> "$RUN_ROOT/$label/run.env"
            ;;
        dcache)
            printf 'metric_basis=L2 request-derived L1D miss proxy\n' \
                >> "$RUN_ROOT/$label/run.env"
            ;;
        tlb)
            printf 'metric_scope=D-side STLB\n' >> "$RUN_ROOT/$label/run.env"
            ;;
    esac
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/$label" \
        --event-names "$names" \
        --expected-calls "$(( ${RANDOM_OUTPUT_LEN:-100} - 1 ))"
done <<'EOF'
topdown|0x76,0xc0,0xc1,0x03aa,0x0487|cycles,instructions,retired_uops,dispatched_uops,frontend_stall_any
branch|0xc0,0xc2,0xc3|instructions,branches,branch_misses
spec_ls|0x03aa,0x0129,0x0229,0x0429,0xc2|dispatched_uops,load_ops,store_ops,load_store_ops,branches
spec_ase|0x03aa,0x0f00|dispatched_uops,fpu_spec_uops
icache|0xc0,0x80,0x81,0x0764,0x0164|instructions,l1i_fetch_windows,l1i_miss_windows,l2i_accesses,l2i_misses
dcache|0xc0,0x0729,0xc860,0x0864,0xf064|instructions,ls_ops,l1d_miss_proxy,l2d_misses,l2d_hits
tlb|0xc0,0x0729,0xff45,0x0f45,0xf045|instructions,ls_ops,l1_dtlb_misses,stlb_hits,stlb_misses
EOF

KPERF_PMU_NAME=amd_l3 RUN_ROOT="$RUN_ROOT" \
    "$COMMON_DIR/run_one.sh" l3 0xff04,0x0106 \
    l3_accesses,l3_misses uncore
printf 'metric_scope=shared L3 domains; not strict thread attribution\n' \
    >> "$RUN_ROOT/l3/run.env"
"$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/l3" \
    --event-names l3_accesses,l3_misses \
    --expected-calls "$(( ${RANDOM_OUTPUT_LEN:-100} - 1 ))"

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" hotspot
"$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT"
"$PYTHON_BIN" "$COMMON_DIR/build_xlsx.py" "$RUN_ROOT" \
    --config "$SCRIPT_DIR/report_config.json" \
    --chip "$(basename "$SCRIPT_DIR")" \
    --version "${VLLM_VERSION_SHORT:-0.26}" \
    --model-short "${MODEL_SHORT:-qwen3}" \
    --input-len "${RANDOM_INPUT_LEN:-7000}" \
    --output-len "${RANDOM_OUTPUT_LEN:-100}"
printf '[hygon] completed: %s\n' "$RUN_ROOT"
