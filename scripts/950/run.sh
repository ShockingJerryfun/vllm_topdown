#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SCRIPT_DIR")
SOURCE_ROOT=$(dirname "$COMMON_DIR")
set -a
source "$COMMON_DIR/config.env"
set +a
RUN_ROOT=${RUN_ROOT:-$SOURCE_ROOT/results/950}
"$PYTHON_BIN" -c 'import openpyxl' >/dev/null 2>&1 || {
    printf 'Missing openpyxl; install scripts/requirements-report.txt\n' >&2
    exit 6
}
[[ ! -e "$RUN_ROOT" ]] || { printf 'Exists: %s\n' "$RUN_ROOT" >&2; exit 2; }
install -d -m 755 "$RUN_ROOT"
: > "$RUN_ROOT/commands.txt"

record_command() {
    local title=$1
    local stdout_path=$2
    local merge_stderr=$3
    local background=$4
    shift 4
    {
        printf '[%s]\n' "$title"
        printf '%q ' "$@"
        if [[ -n "$stdout_path" ]]; then
            printf '> %q' "$stdout_path"
            [[ "$merge_stderr" == 1 ]] && printf ' 2>&1'
        fi
        [[ "$background" == 1 ]] && printf ' &'
        printf '\n\n'
    } >> "$RUN_ROOT/commands.txt"
}

if [[ -z "$VLLM_SITE" ]]; then
    VLLM_SITE=$("$PYTHON_BIN" -c 'import importlib.util; print(next(iter(importlib.util.find_spec("vllm").submodule_search_locations)))')
fi
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
    printf '\nmidr_el1='
    cat /sys/devices/system/cpu/cpu0/regs/identification/midr_el1 2>/dev/null || true
    printf '\nperf_event_paranoid='
    cat /proc/sys/kernel/perf_event_paranoid
    printf 'nmi_watchdog='
    cat /proc/sys/kernel/nmi_watchdog
} > "$RUN_ROOT/platform.txt"
RUNTIME_COMMAND=(
    env
    PYTHONPATH="$VLLM_PYTHONPATH"
    VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
    "$PYTHON_BIN" -c
    'import os, sys, torch, vllm; from vllm.v1.worker.gpu import model_runner; from vllm.model_executor.models import qwen3; print(sys.version); print(vllm.__version__); print(torch.__version__); print(os.path.realpath(vllm.__file__)); print(os.path.realpath(model_runner.__file__)); print(os.path.realpath(qwen3.__file__))'
)
record_command "runtime identity" "$RUN_ROOT/runtime.txt" 0 0 \
    "${RUNTIME_COMMAND[@]}"
"${RUNTIME_COMMAND[@]}" > "$RUN_ROOT/runtime.txt"

EXPECTED_CALLS=$(( RANDOM_OUTPUT_LEN - 1 ))

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" time
PARSE_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/time"
    --mode time
    --expected-calls "$EXPECTED_CALLS"
)
record_command "time parse" "" 0 0 "${PARSE_COMMAND[@]}"
"${PARSE_COMMAND[@]}"

while IFS='|' read -r label codes; do
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" "$label" "$codes" "$codes"
    PARSE_COMMAND=(
        "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/$label"
        --event-names "$codes"
        --expected-calls "$EXPECTED_CALLS"
    )
    record_command "$label parse" "" 0 0 "${PARSE_COMMAND[@]}"
    "${PARSE_COMMAND[@]}"
done <<EOF
topdown|$EVENTS_950_TOPDOWN
icache|$EVENTS_950_ICACHE
dcache|$EVENTS_950_DCACHE
l3|$EVENTS_950_L3
tlb1|$EVENTS_950_TLB1
tlb2|$EVENTS_950_TLB2
branch|$EVENTS_950_BRANCH
imix|$EVENTS_950_IMIX
imix2|$EVENTS_950_IMIX2
EOF

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" hotspot
SUMMARY_COMMAND=("$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT")
record_command "summary" "" 0 0 "${SUMMARY_COMMAND[@]}"
"${SUMMARY_COMMAND[@]}"

BUILD_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/build_xlsx.py" "$RUN_ROOT"
    --config "$SCRIPT_DIR/report_config.json"
    --chip "$(basename "$SCRIPT_DIR")"
    --version "$VLLM_VERSION_SHORT"
    --model-short "$MODEL_SHORT"
    --input-len "$RANDOM_INPUT_LEN"
    --output-len "$RANDOM_OUTPUT_LEN"
)
record_command "Excel report" "" 0 0 "${BUILD_COMMAND[@]}"
"${BUILD_COMMAND[@]}"
printf '[950] completed: %s\n' "$RUN_ROOT"
