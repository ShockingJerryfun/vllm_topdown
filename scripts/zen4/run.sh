#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SCRIPT_DIR")
SOURCE_ROOT=$(dirname "$COMMON_DIR")
set -a
source "$COMMON_DIR/config.env"
set +a
COLLECTION_PROFILE=${COLLECTION_PROFILE:-full}
case "$COLLECTION_PROFILE" in full|end_to_end) ;; *) exit 2 ;; esac
[[ ${FREQUENCY_ENABLE:-0} == 0 ]] || {
    printf 'Zen 4 does not support the Arm DevKit frequency collector\n' >&2
    exit 2
}
"$PYTHON_BIN" "$SCRIPT_DIR/identity.py"
RUN_ROOT=${RUN_ROOT:-$SOURCE_ROOT/results/zen4}
"$PYTHON_BIN" -c 'import openpyxl' >/dev/null 2>&1 || {
    printf 'Missing openpyxl; install scripts/requirements-report.txt\n' >&2
    exit 6
}
[[ ! -e "$RUN_ROOT" ]] || { printf 'Exists: %s\n' "$RUN_ROOT" >&2; exit 2; }
install -d -m 755 "$RUN_ROOT"
: > "$RUN_ROOT/commands.txt"
GROUP_LIST=$("$PYTHON_BIN" "$SCRIPT_DIR/groups.py" "$RUN_ROOT")
REPORT_CONFIG="$RUN_ROOT/report_config.json"
if [[ ${ZEN4_L3_ENABLE:-0} == 1 ]]; then
    export KPERF_L3_FILTERED=1
    KPERF_L3_CPU=$(cat "$RUN_ROOT/l3_cpu.txt")
    export KPERF_L3_CPU
fi

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
source "$COMMON_DIR/session.sh"
cp -rs "$VLLM_SITE" "$RUNTIME/vllm"
while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_ROOT/vllm/"}
    target="$RUNTIME/vllm/$relative"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$file" "$target"
done < <(find "$SOURCE_ROOT/vllm" -type f -print0)
ln -s "$SOURCE_ROOT/kperf_instrument.py" "$RUNTIME/kperf_instrument.py"
export SOURCE_ROOT VLLM_PYTHONPATH=$RUNTIME

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
start_session
END_TO_END_STAGE=execute_model_to_sample_tokens
END_TO_END_ROOT="$RUN_ROOT/end_to_end"

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" benchmark_before
if [[ "$COLLECTION_PROFILE" == full ]]; then
RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" time
PARSE_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/time"
    --mode time
    --expected-calls "$EXPECTED_CALLS"
)
record_command "time parse" "" 0 0 "${PARSE_COMMAND[@]}"
"${PARSE_COMMAND[@]}"

while IFS='|' read -r label codes names scope; do
    [[ -n "$label" ]] || continue
    KPERF_PMU_NAME=amd_l3 RUN_ROOT="$RUN_ROOT" \
        "$COMMON_DIR/run_one.sh" "$label" "$codes" "$names" "$scope"
    PARSE_COMMAND=(
        "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/$label"
        --event-names "$names"
        --expected-calls "$EXPECTED_CALLS"
        --require-unmultiplexed
    )
    record_command "$label parse" "" 0 0 "${PARSE_COMMAND[@]}"
    "${PARSE_COMMAND[@]}"
done <<< "$GROUP_LIST"

fi
printf '%s\n' "$COLLECTION_PROFILE" > "$RUN_ROOT/collection_profile"

install -d -m 755 "$END_TO_END_ROOT"
: > "$END_TO_END_ROOT/commands.txt"
KPERF_TARGET="$END_TO_END_STAGE" KPERF_QUALIFIER=run_fullgraph \
    RUN_ROOT="$END_TO_END_ROOT" "$COMMON_DIR/run_one.sh" time
PARSE_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$END_TO_END_ROOT/time"
    --mode time
    --profile end_to_end
    --expected-calls "$EXPECTED_CALLS"
)
record_command "end-to-end time parse" "" 0 0 "${PARSE_COMMAND[@]}"
"${PARSE_COMMAND[@]}"

while IFS='|' read -r label codes names scope; do
    [[ -n "$label" ]] || continue
    KPERF_TARGET="$END_TO_END_STAGE" KPERF_QUALIFIER=run_fullgraph \
        KPERF_PMU_NAME=amd_l3 RUN_ROOT="$END_TO_END_ROOT" \
        "$COMMON_DIR/run_one.sh" "$label" "$codes" "$names" "$scope"
    PARSE_COMMAND=(
        "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$END_TO_END_ROOT/$label"
        --event-names "$names"
        --profile end_to_end
        --expected-calls "$EXPECTED_CALLS"
        --require-unmultiplexed
    )
    record_command "end-to-end $label parse" "" 0 0 "${PARSE_COMMAND[@]}"
    "${PARSE_COMMAND[@]}"
done <<< "$GROUP_LIST"

if [[ "$COLLECTION_PROFILE" == full ]]; then
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" hotspot
fi
RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" benchmark_after
stop_session
SUMMARY_COMMAND=("$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT")
record_command "summary" "" 0 0 "${SUMMARY_COMMAND[@]}"
"${SUMMARY_COMMAND[@]}"

BUILD_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/build_xlsx.py" "$RUN_ROOT"
    --config "$REPORT_CONFIG"
    --chip "$(basename "$SCRIPT_DIR")"
    --version "$VLLM_VERSION_SHORT"
    --model-short "$MODEL_SHORT"
    --input-len "$RANDOM_INPUT_LEN"
    --output-len "$RANDOM_OUTPUT_LEN"
    --compact-report
    --include-end-to-end
)
record_command "Excel report" "" 0 0 "${BUILD_COMMAND[@]}"
"${BUILD_COMMAND[@]}"
