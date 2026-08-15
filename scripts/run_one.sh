#!/usr/bin/env bash

set -Eeuo pipefail

LABEL=${1:?usage: run_one.sh LABEL [EVENT_CODES]}
CODES=${2-}
RUN_ROOT=${RUN_ROOT:?set RUN_ROOT}
MODEL=${MODEL:?set MODEL}
VLLM_BIN=${VLLM_BIN:-vllm}
VLLM_PYTHONPATH=${VLLM_PYTHONPATH:?set VLLM_PYTHONPATH}
SERVED_MODEL=${SERVED_MODEL:-qwen25-1_5b}
CPU_ID=${CPU_ID:-250}
GPU_ID=${GPU_ID:-0}
PORT=${PORT:-18026}
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

trap cleanup EXIT INT TERM

[[ ! -e "$RUN_DIR" ]] || { printf 'Exists: %s\n' "$RUN_DIR" >&2; exit 2; }
ss -ltn | grep -qE ":${PORT}[[:space:]]" && { printf 'Port in use: %s\n' "$PORT" >&2; exit 3; }
install -d -m 755 "$RUN_DIR"

{
    printf 'version=0.26.0\n'
    printf 'label=%s\n' "$LABEL"
    printf 'events=%s\n' "$CODES"
    printf 'model=%s\n' "$MODEL"
    printf 'source=%s\n' "$VLLM_PYTHONPATH"
} > "$RUN_DIR/run.env"

KPERF_ENV=(KPERF_ENABLE=0)
if [[ -n "$CODES" ]]; then
    KPERF_ENV=(
        KPERF_ENABLE=1
        KPERF_RAW_EVENTS="$CODES"
        KPERF_EVENT_NAMES="$CODES"
    )
fi

setsid taskset -c "$CPU_ID" env \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="$VLLM_PYTHONPATH" \
    CUDA_VISIBLE_DEVICES="$GPU_ID" \
    VLLM_USE_V1=1 \
    VLLM_USE_V2_MODEL_RUNNER=1 \
    "${KPERF_ENV[@]}" \
    "$VLLM_BIN" serve "$MODEL" \
    --served-model-name "$SERVED_MODEL" \
    --host 127.0.0.1 \
    --port "$PORT" \
    --max-model-len 8192 \
    --max-num-seqs 1 \
    --max-num-batched-tokens 8192 \
    --tensor-parallel-size 1 \
    --data-parallel-size 1 \
    --dtype bfloat16 \
    --gpu-memory-utilization 0.9 \
    --enforce-eager \
    --enable-prefix-caching \
    --enable-chunked-prefill \
    --seed 0 \
    > "$RUN_DIR/server.log" 2>&1 &
SERVICE_PID=$!

READY=0
for _ in $(seq 1 180); do
    kill -0 "$SERVICE_PID" 2>/dev/null || break
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
        READY=1
        break
    fi
    sleep 2
done
[[ "$READY" -eq 1 ]] || { tail -n 120 "$RUN_DIR/server.log" >&2; exit 4; }

sleep 2
START_LINE=$(( $(wc -l < "$RUN_DIR/server.log") + 1 ))

if [[ "$LABEL" == hotspot ]]; then
    perf record -C "$CPU_ID" -e cycles:u -F 999 -g --call-graph dwarf \
        -o "$RUN_DIR/perf.data" -- sleep 600 > "$RUN_DIR/perf.log" 2>&1 &
    PERF_PID=$!
    sleep 1
fi

set +e
env \
    PYTHONPATH="$VLLM_PYTHONPATH" \
    CUDA_VISIBLE_DEVICES="$GPU_ID" \
    VLLM_USE_V1=1 \
    VLLM_USE_V2_MODEL_RUNNER=1 \
    "$VLLM_BIN" bench serve \
    --backend openai \
    --base-url "http://127.0.0.1:${PORT}" \
    --endpoint /v1/completions \
    --model "$SERVED_MODEL" \
    --tokenizer "$MODEL" \
    --dataset-name random \
    --random-input-len 7000 \
    --random-output-len 100 \
    --random-range-ratio 0 \
    --num-prompts 1 \
    --num-warmups 0 \
    --ready-check-timeout-sec 0 \
    --max-concurrency 1 \
    --request-rate inf \
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
