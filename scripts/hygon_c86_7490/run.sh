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

while IFS='|' read -r label codes names; do
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" "$label" "$codes" "$names"
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/$label" \
        --event-names "$names" \
        --expected-calls "$(( ${RANDOM_OUTPUT_LEN:-100} - 1 ))"
done <<'EOF'
base|0x76,0xc0,0xc2,0xc3,0xc1|cycles,instructions,branches,branch_misses,retired_uops
uops_ls|0x76,0xc0,0x03aa,0xc1,0x0729|cycles,instructions,dispatched_uops,retired_uops,ls_ops_dispatched
frontend|0x76,0xc0,0x0287,0x0187,0x81|cycles,instructions,ic_dq_empty,ic_backpressure,l1i_fetch_misses
backend|0x76,0xc0,0x40af,0x20af,0x10af|cycles,instructions,retire_token_stalls,agsq_token_stalls,alu_token_stalls
dcache|0xc0,0x40,0xe860,0x0864,0xf064|instructions,l1d_accesses,l2_request_activity,l2_demand_misses,l2_demand_hits
dtlb|0xc0,0xff45,0x0f45,0xf045,0x0346|instructions,l1_dtlb_misses,dtlb_l2_hits,dtlb_l2_misses,data_page_walks
EOF

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
