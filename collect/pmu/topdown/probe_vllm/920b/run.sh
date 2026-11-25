#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SCRIPT_DIR")
ADAPTER_DIR=$(dirname "$COMMON_DIR")
set -a
source "$COMMON_DIR/config.env"
set +a
: "${TOPDOWN_HOME:?invoke adapters/vllm/run.sh}"
SOURCE_ROOT=${VLLM_SOURCE_ROOT:?set VLLM_SOURCE_ROOT to the instrumented vLLM source}
[[ -z ${CODE_PAGE_CONDITION:-} ]] || {
    printf 'Private code-page conditions require the original external supervisor; omit CODE_PAGE_CONDITION for this adapter\n' >&2
    exit 2
}
COLLECTION_PROFILE=${COLLECTION_PROFILE:-full}
case "$COLLECTION_PROFILE" in full|end_to_end) ;; *) exit 2 ;; esac
TOPDOWN_PMU_PROFILE=${TOPDOWN_PMU_PROFILE:-920b}
case "$TOPDOWN_PMU_PROFILE" in 920b|zen4) ;; *) printf 'Unsupported PMU profile: %s\n' "$TOPDOWN_PMU_PROFILE" >&2; exit 2 ;; esac
export TOPDOWN_PMU_PROFILE
PROFILE_ARGS=()
STRICT_PMU_ARGS=()
if [[ "$TOPDOWN_PMU_PROFILE" == zen4 ]]; then
    [[ ${ZERO_PORT_HOTSPOT:-0} == 0 && ${FREQUENCY_ENABLE:-0} == 0 && ${SPE_ENABLE:-0} == 0 ]] || {
        printf 'Zen 4 does not support the 920B zero-port, DevKit frequency, or SPE paths\n' >&2
        exit 2
    }
    PROFILE_ARGS=(--pmu-profile zen4)
    STRICT_PMU_ARGS=(--require-unmultiplexed)
    PMU_IDENTITY=$("$PYTHON_BIN" "$COMMON_DIR/pmu_identity.py" zen4)
fi
RUN_ROOT=${RUN_ROOT:-$SOURCE_ROOT/results/$TOPDOWN_PMU_PROFILE}
"$PYTHON_BIN" -c 'import openpyxl' >/dev/null 2>&1 || {
    printf 'Missing openpyxl; install scripts/requirements-report.txt\n' >&2
    exit 6
}
[[ ! -e "$RUN_ROOT" ]] || { printf 'Exists: %s\n' "$RUN_ROOT" >&2; exit 2; }
install -d -m 755 "$RUN_ROOT"
: > "$RUN_ROOT/commands.txt"
if [[ "$TOPDOWN_PMU_PROFILE" == zen4 ]]; then
    printf '%s\n' "$TOPDOWN_PMU_PROFILE" > "$RUN_ROOT/pmu_profile"
    printf '%s\n' "$PMU_IDENTITY" > "$RUN_ROOT/pmu_identity.json"
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
RUNTIME=$(mktemp -d "${TOPDOWN_RUNTIME_BASE:-$RUN_ROOT}/topdown-vllm.XXXXXX")
source "$COMMON_DIR/session.sh"
cp -rs "$VLLM_SITE" "$RUNTIME/vllm"
while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_ROOT/vllm/"}
    target="$RUNTIME/vllm/$relative"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$file" "$target"
done < <(find -L "$SOURCE_ROOT/vllm" -type f -print0)
ln -s "$ADAPTER_DIR/kperf_instrument.py" "$RUNTIME/kperf_instrument.py"
ln -s "$TOPDOWN_HOME/topdown" "$RUNTIME/topdown"
ln -sfn "$COMMON_DIR/numa_utils.py" "$RUNTIME/vllm/utils/numa_utils.py"
export SOURCE_ROOT VLLM_PYTHONPATH=$RUNTIME

RUNTIME_COMMAND=(
    env
    PYTHONPATH="$VLLM_PYTHONPATH"
    VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
    "$PYTHON_BIN" -c
    'import os, sys, torch, vllm; from vllm.v1.worker.gpu import model_runner; import topdown.probe; print(sys.version); print(vllm.__version__); print(torch.__version__); print(os.path.realpath(vllm.__file__)); print(os.path.realpath(model_runner.__file__)); print(os.path.realpath(topdown.probe.__file__))'
)
record_command "runtime identity" "$RUN_ROOT/runtime.txt" 0 0 \
    "${RUNTIME_COMMAND[@]}"
"${RUNTIME_COMMAND[@]}" > "$RUN_ROOT/runtime.txt"

if [[ ${ANON_THP_MODE:-off} == off ]]; then
    install -d -m 755 "$RUN_ROOT/tools"
    "${CC:-cc}" "$COMMON_DIR/thp_exec.c" -O2 -o "$RUN_ROOT/tools/thp_exec"
    export TOPDOWN_THP_EXEC="$RUN_ROOT/tools/thp_exec"
fi
GROUP_LIST=$("$PYTHON_BIN" -m topdown.groups "$TOPDOWN_PMU_PROFILE")
EXPECTED_CALLS=$(( RANDOM_OUTPUT_LEN - 1 ))
start_session
RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" benchmark_before
END_TO_END_STAGE=execute_model_to_sample_tokens
END_TO_END_ROOT="$RUN_ROOT/end_to_end"

if [[ "$COLLECTION_PROFILE" == full ]]; then
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
        "${STRICT_PMU_ARGS[@]}"
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

while IFS='|' read -r label codes; do
    KPERF_TARGET="$END_TO_END_STAGE" KPERF_QUALIFIER=run_fullgraph \
        RUN_ROOT="$END_TO_END_ROOT" \
        "$COMMON_DIR/run_one.sh" "$label" "$codes" "$codes"
    PARSE_COMMAND=(
        "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$END_TO_END_ROOT/$label"
        --event-names "$codes"
        --profile end_to_end
        --expected-calls "$EXPECTED_CALLS"
        "${STRICT_PMU_ARGS[@]}"
    )
    record_command "end-to-end $label parse" "" 0 0 "${PARSE_COMMAND[@]}"
    "${PARSE_COMMAND[@]}"
done <<< "$GROUP_LIST"

if [[ "$COLLECTION_PROFILE" == full ]]; then
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" hotspot
    if [[ ${ZERO_PORT_HOTSPOT:-0} == 1 ]]; then
        HOTSPOT_EVENT=r700b:u RUN_ROOT="$RUN_ROOT/zero_ports" "$COMMON_DIR/run_one.sh" hotspot
    fi
fi
if [[ ${FREQUENCY_ENABLE:-0} == 1 ]]; then
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" frequency
fi
RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" benchmark_after
stop_session
SUMMARY_COMMAND=("$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT" "${PROFILE_ARGS[@]}")
record_command "summary" "" 0 0 "${SUMMARY_COMMAND[@]}"
"${SUMMARY_COMMAND[@]}"

BUILD_COMMAND=(
    "$PYTHON_BIN" "$COMMON_DIR/build_xlsx.py" "$RUN_ROOT"
    --config "$COMMON_DIR/$TOPDOWN_PMU_PROFILE/report_config.json"
    --chip "$TOPDOWN_PMU_PROFILE"
    --version "$VLLM_VERSION_SHORT"
    --model-short "$MODEL_SHORT"
    --input-len "$RANDOM_INPUT_LEN"
    --output-len "$RANDOM_OUTPUT_LEN"
    --compact-report
    --include-end-to-end
    "${PROFILE_ARGS[@]}"
)
record_command "Excel report" "" 0 0 "${BUILD_COMMAND[@]}"
"${BUILD_COMMAND[@]}"
