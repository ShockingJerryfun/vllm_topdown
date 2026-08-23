#!/usr/bin/env bash

set -Eeuo pipefail

LABEL=${1:?usage: run_one.sh LABEL [EVENT_CODES] [EVENT_NAMES]}
CODES=${2-}
NAMES=${3:-$CODES}
RUN_ROOT=${RUN_ROOT:?set RUN_ROOT}
MODEL=${MODEL:?set MODEL}
VLLM_BIN=${VLLM_BIN:-vllm}
VLLM_PYTHONPATH=${VLLM_PYTHONPATH:?set VLLM_PYTHONPATH}
SOURCE_ROOT=${SOURCE_ROOT:-$VLLM_PYTHONPATH}
export -n VLLM_BIN VLLM_PYTHONPATH VLLM_VERSION_SHORT
SERVED_MODEL=${SERVED_MODEL:-qwen3-8b}
GPU_ID=${GPU_ID:-0}
PORT=${PORT:-18026}
MAX_MODEL_LEN=${MAX_MODEL_LEN:-16384}
MAX_NUM_BATCHED_TOKENS=${MAX_NUM_BATCHED_TOKENS:-163840}
GPU_MEMORY_UTILIZATION=${GPU_MEMORY_UTILIZATION:-0.8}
RANDOM_INPUT_LEN=${RANDOM_INPUT_LEN:-7000}
RANDOM_OUTPUT_LEN=${RANDOM_OUTPUT_LEN:-100}
REQUEST_RATE=${REQUEST_RATE:-1}
NUM_PROMPTS=${NUM_PROMPTS:-1}
EXECUTION_MODE=graph
[[ " ${SERVER_FLAGS:-} " == *" --enforce-eager "* ]] && EXECUTION_MODE=eager
RUN_DIR="$RUN_ROOT/$LABEL"
SERVICE_PID=""
PERF_PID=""

cleanup() {
    if [[ -n "$PERF_PID" ]] && kill -0 "$PERF_PID" 2>/dev/null; then
        kill -INT "$PERF_PID" 2>/dev/null || true
        wait "$PERF_PID" 2>/dev/null || true
    fi
    if [[ -n "$SERVICE_PID" ]] && kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
        kill -TERM -- "-$SERVICE_PID" 2>/dev/null || true
        for _ in $(seq 1 30); do
            kill -0 -- "-$SERVICE_PID" 2>/dev/null || break
            sleep 1
        done
        kill -KILL -- "-$SERVICE_PID" 2>/dev/null || true
        wait "$SERVICE_PID" 2>/dev/null || true
    fi
}

process_tree() {
    local queue=("$SERVICE_PID")
    local pids=("$SERVICE_PID")
    local parent
    local children
    for parent in "${queue[@]}"; do
        mapfile -t children < <(pgrep -P "$parent" 2>/dev/null || true)
        queue+=("${children[@]}")
        pids+=("${children[@]}")
    done
    local IFS=,
    printf '%s' "${pids[*]}"
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
    COLLECT_MODE=pmu
    KPERF_ENV=(
        KPERF_ENABLE=1
        KPERF_MODE=pmu
        KPERF_RAW_EVENTS="$CODES"
        KPERF_EVENT_NAMES="$NAMES"
    )
fi

{
    printf 'version=0.26.0\n'
    printf 'label=%s\n' "$LABEL"
    printf 'mode=%s\n' "$COLLECT_MODE"
    printf 'events=%s\n' "$CODES"
    printf 'names=%s\n' "$NAMES"
    printf 'model=%s\n' "$MODEL"
    printf 'source=%s\n' "$SOURCE_ROOT"
    printf 'execution_mode=%s\n' "$EXECUTION_MODE"
    printf 'server_flags=%s\n' "${SERVER_FLAGS:-}"
} > "$RUN_DIR/run.env"

read -r -a EXTRA_SERVER_FLAGS <<< "${SERVER_FLAGS:-}"

setsid env \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="$VLLM_PYTHONPATH" \
    CUDA_VISIBLE_DEVICES="$GPU_ID" \
    VLLM_USE_V2_MODEL_RUNNER=1 \
    "${KPERF_ENV[@]}" \
    "$VLLM_BIN" serve "$MODEL" \
    --served-model-name "$SERVED_MODEL" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --block-size 16 \
    --max-model-len "$MAX_MODEL_LEN" \
    --max-num-seqs 1 \
    --max-num-batched-tokens "$MAX_NUM_BATCHED_TOKENS" \
    --tensor-parallel-size 1 \
    --data-parallel-size 1 \
    --dtype bfloat16 \
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
    --no-enable-prefix-caching \
    --seed 0 \
    "${EXTRA_SERVER_FLAGS[@]}" \
    > "$RUN_DIR/server.log" 2>&1 &
SERVICE_PID=$!

READY=0
for _ in $(seq 1 180); do
    kill -0 "$SERVICE_PID" 2>/dev/null || break
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 2
done
[[ "$READY" -eq 1 ]] || { tail -n 120 "$RUN_DIR/server.log" >&2; exit 4; }

sleep 2
START_LINE=$(( $(wc -l < "$RUN_DIR/server.log") + 1 ))

if [[ "$LABEL" == hotspot ]]; then
    TARGET_PIDS=$(process_tree)
    perf record -e cycles:u -F 999 -g --call-graph dwarf \
        -p "$TARGET_PIDS" -o "$RUN_DIR/perf.data" \
        > "$RUN_DIR/perf.log" 2>&1 &
    PERF_PID=$!
    sleep 1
fi

set +e
env \
    PYTHONPATH="$VLLM_PYTHONPATH" \
    CUDA_VISIBLE_DEVICES="$GPU_ID" \
    VLLM_USE_V2_MODEL_RUNNER=1 \
    "$VLLM_BIN" bench serve \
    --backend openai \
    --base-url "http://127.0.0.1:${PORT}" \
    --endpoint /v1/completions \
    --model "$SERVED_MODEL" \
    --tokenizer "$MODEL" \
    --dataset-name random \
    --random-input-len "$RANDOM_INPUT_LEN" \
    --random-output-len "$RANDOM_OUTPUT_LEN" \
    --random-range-ratio 0 \
    --num-prompts "$NUM_PROMPTS" \
    --num-warmups 0 \
    --ready-check-timeout-sec 0 \
    --max-concurrency 1 \
    --request-rate "$REQUEST_RATE" \
    --ignore-eos \
    --temperature 0 \
    --seed 0 \
    > "$RUN_DIR/benchmark.log" 2>&1
BENCH_RC=$?
set -e

if [[ -n "$PERF_PID" ]]; then
    kill -INT "$PERF_PID" 2>/dev/null || true
    wait "$PERF_PID" 2>/dev/null || true
    PERF_PID=""
fi

sleep 2
END_LINE=$(wc -l < "$RUN_DIR/server.log")
cleanup
SERVICE_PID=""
trap - EXIT INT TERM
sed -n "${START_LINE},${END_LINE}p" "$RUN_DIR/server.log" > "$RUN_DIR/measurement.log"

[[ "$BENCH_RC" -eq 0 ]] || { tail -n 120 "$RUN_DIR/benchmark.log" >&2; exit 5; }
if [[ "$LABEL" == hotspot ]]; then
    perf report --stdio --no-children -i "$RUN_DIR/perf.data" \
        > "$RUN_DIR/perf_report.txt" 2>&1
fi

printf 'completed %s\n' "$LABEL"
