#!/usr/bin/env bash

set -Eeuo pipefail

LABEL=${1:?usage: run_one.sh LABEL [EVENT_CODES] [EVENT_NAMES] [PMU_SCOPE]}
CODES=${2-}
NAMES=${3:-$CODES}
PMU_SCOPE=${4:-thread}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
set -a
source "$SCRIPT_DIR/config.env"
set +a
RUN_ROOT=${RUN_ROOT:?set RUN_ROOT}
VLLM_PYTHONPATH=${VLLM_PYTHONPATH:?set VLLM_PYTHONPATH}
SOURCE_ROOT=${SOURCE_ROOT:-$VLLM_PYTHONPATH}
export -n VLLM_BIN VLLM_PYTHONPATH VLLM_VERSION_SHORT
EXECUTION_MODE=graph
[[ " $SERVER_FLAGS " == *" --enforce-eager "* ]] && EXECUTION_MODE=eager
RUN_DIR="$RUN_ROOT/$LABEL"
SERVICE_PID=""
PERF_PID=""

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

cleanup() {
    if [[ -n "$PERF_PID" ]] && kill -0 "$PERF_PID" 2>/dev/null; then
        kill -INT "$PERF_PID" 2>/dev/null || true
        wait "$PERF_PID" 2>/dev/null || true
    fi
    if [[ -n "$SERVICE_PID" ]] && kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
        kill -TERM -- "-$SERVICE_PID" 2>/dev/null || true
        for _ in $(seq 1 "$SHUTDOWN_ATTEMPTS"); do
            kill -0 -- "-$SERVICE_PID" 2>/dev/null || break
            sleep "$SHUTDOWN_INTERVAL"
        done
        kill -KILL -- "-$SERVICE_PID" 2>/dev/null || true
        wait "$SERVICE_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

[[ ! -e "$RUN_DIR" ]] || { printf 'Exists: %s\n' "$RUN_DIR" >&2; exit 2; }
install -d -m 755 "$RUN_DIR"

COLLECT_MODE=disabled
KPERF_ENV=(KPERF_ENABLE=0)
if [[ "$LABEL" == time ]]; then
    COLLECT_MODE=time
    KPERF_ENV=(KPERF_ENABLE=1 KPERF_MODE=time)
elif [[ -n "$CODES" ]]; then
    [[ "$PMU_SCOPE" == thread || "$PMU_SCOPE" == uncore ]] || {
        printf 'Unsupported PMU scope: %s\n' "$PMU_SCOPE" >&2
        exit 7
    }
    COLLECT_MODE=pmu
    KPERF_ENV=(
        KPERF_ENABLE=1
        KPERF_MODE=pmu
        KPERF_SCOPE="$PMU_SCOPE"
        KPERF_RAW_EVENTS="$CODES"
        KPERF_EVENT_NAMES="$NAMES"
    )
    if [[ "$PMU_SCOPE" == uncore ]]; then
        : "${KPERF_PMU_NAME:?set KPERF_PMU_NAME for uncore collection}"
        KPERF_ENV+=(KPERF_PMU_NAME="$KPERF_PMU_NAME")
    fi
fi

{
    printf 'version=%s\n' "$VLLM_VERSION"
    printf 'label=%s\n' "$LABEL"
    printf 'mode=%s\n' "$COLLECT_MODE"
    printf 'pmu_scope=%s\n' "$PMU_SCOPE"
    printf 'pmu_name=%s\n' "${KPERF_PMU_NAME:-}"
    printf 'events=%s\n' "$CODES"
    printf 'names=%s\n' "$NAMES"
    printf 'model=%s\n' "$MODEL"
    printf 'source=%s\n' "$SOURCE_ROOT"
    printf 'execution_mode=%s\n' "$EXECUTION_MODE"
    printf 'server_flags=%s\n' "$SERVER_FLAGS"
} > "$RUN_DIR/run.env"

read -r -a EXTRA_SERVER_FLAGS <<< "$SERVER_FLAGS"
read -r -a PREFIX_CACHING_ARGS <<< "$PREFIX_CACHING_FLAG"
read -r -a IGNORE_EOS_ARGS <<< "$IGNORE_EOS_FLAG"
read -r -a PERF_REPORT_ARGS <<< "$PERF_REPORT_FLAGS"

SERVER_COMMAND=(
    setsid env
    PYTHONUNBUFFERED=1
    PYTHONPATH="$VLLM_PYTHONPATH"
    CUDA_VISIBLE_DEVICES="$GPU_ID"
    VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
    "${KPERF_ENV[@]}"
    "$VLLM_BIN" serve "$MODEL"
    --served-model-name "$SERVED_MODEL"
    --host "$SERVER_HOST"
    --port "$PORT"
    --block-size "$BLOCK_SIZE"
    --max-model-len "$MAX_MODEL_LEN"
    --max-num-seqs "$MAX_NUM_SEQS"
    --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS"
    --tensor-parallel-size "$TENSOR_PARALLEL_SIZE"
    --data-parallel-size "$DATA_PARALLEL_SIZE"
    --dtype "$DTYPE"
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION"
    "${PREFIX_CACHING_ARGS[@]}"
    --seed "$SERVER_SEED"
    "${EXTRA_SERVER_FLAGS[@]}"
)
record_command "$LABEL vLLM serve" "$RUN_DIR/server.log" 1 1 \
    "${SERVER_COMMAND[@]}"
"${SERVER_COMMAND[@]}" > "$RUN_DIR/server.log" 2>&1 &
SERVICE_PID=$!

READY=0
for _ in $(seq 1 "$READY_CHECK_ATTEMPTS"); do
    kill -0 "$SERVICE_PID" 2>/dev/null || break
    if curl -fsS "http://${SERVER_HOST}:${PORT}${HEALTH_ENDPOINT}" \
        >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep "$READY_CHECK_INTERVAL"
done
[[ "$READY" -eq 1 ]] || { tail -n 120 "$RUN_DIR/server.log" >&2; exit 4; }

sleep "$SERVICE_SETTLE_SECONDS"
START_LINE=$(( $(wc -l < "$RUN_DIR/server.log") + 1 ))

if [[ "$LABEL" == hotspot ]]; then
    WORKER_PID=$(pgrep -f "$HOTSPOT_WORKER_PATTERN" 2>/dev/null | head -1 || true)
    [[ -n "$WORKER_PID" ]] || {
        printf 'No process matched: %s\n' "$HOTSPOT_WORKER_PATTERN" >&2
        exit 8
    }
    PERF_COMMAND=(
        perf record
        -e "$PERF_EVENT"
        -c "$PERF_PERIOD"
        -o "$RUN_DIR/perf.data"
        -p "$WORKER_PID"
    )
    record_command "$LABEL perf record" "$RUN_DIR/perf.log" 1 1 \
        "PYTHONPERFSUPPORT=$PYTHON_PERF_SUPPORT" "${PERF_COMMAND[@]}"
    PYTHONPERFSUPPORT="$PYTHON_PERF_SUPPORT" \
        "${PERF_COMMAND[@]}" > "$RUN_DIR/perf.log" 2>&1 &
    PERF_PID=$!
    sleep "$PERF_SETTLE_SECONDS"
fi

BENCHMARK_COMMAND=(
    env
    PYTHONPATH="$VLLM_PYTHONPATH"
    CUDA_VISIBLE_DEVICES="$GPU_ID"
    VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
    "$VLLM_BIN" bench serve
    --backend "$BENCH_BACKEND"
    --base-url "http://${SERVER_HOST}:${PORT}"
    --endpoint "$BENCH_ENDPOINT"
    --model "$SERVED_MODEL"
    --tokenizer "$MODEL"
    --dataset-name "$DATASET_NAME"
    --random-input-len "$RANDOM_INPUT_LEN"
    --random-output-len "$RANDOM_OUTPUT_LEN"
    --random-range-ratio "$RANDOM_RANGE_RATIO"
    --num-prompts "$NUM_PROMPTS"
    --num-warmups "$NUM_WARMUPS"
    --ready-check-timeout-sec "$READY_CHECK_TIMEOUT_SEC"
    --max-concurrency "$MAX_CONCURRENCY"
    --request-rate "$REQUEST_RATE"
    "${IGNORE_EOS_ARGS[@]}"
    --temperature "$TEMPERATURE"
    --seed "$BENCH_SEED"
)
record_command "$LABEL vLLM benchmark" "$RUN_DIR/benchmark.log" 1 0 \
    "${BENCHMARK_COMMAND[@]}"
set +e
"${BENCHMARK_COMMAND[@]}" > "$RUN_DIR/benchmark.log" 2>&1
BENCH_RC=$?
set -e

if [[ -n "$PERF_PID" ]]; then
    kill -INT "$PERF_PID" 2>/dev/null || true
    wait "$PERF_PID" 2>/dev/null || true
    PERF_PID=""
fi

sleep "$SERVICE_SETTLE_SECONDS"
END_LINE=$(wc -l < "$RUN_DIR/server.log")
cleanup
SERVICE_PID=""
trap - EXIT INT TERM
sed -n "${START_LINE},${END_LINE}p" "$RUN_DIR/server.log" > "$RUN_DIR/measurement.log"

[[ "$BENCH_RC" -eq 0 ]] || { tail -n 120 "$RUN_DIR/benchmark.log" >&2; exit 5; }
if [[ "$LABEL" == hotspot ]]; then
    PERF_REPORT_COMMAND=(
        perf report
        "${PERF_REPORT_ARGS[@]}"
        -i "$RUN_DIR/perf.data"
    )
    record_command "$LABEL perf report" "$RUN_DIR/perf_report.txt" 1 0 \
        "${PERF_REPORT_COMMAND[@]}"
    "${PERF_REPORT_COMMAND[@]}" > "$RUN_DIR/perf_report.txt" 2>&1
fi

printf 'completed %s\n' "$LABEL"
