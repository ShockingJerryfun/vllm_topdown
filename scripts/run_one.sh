#!/usr/bin/env bash

set -Eeuo pipefail

EVENT_LABEL=${1:?usage: run_one.sh EVENT_LABEL EVENT_CODES}
EVENT_CODES=${2:?usage: run_one.sh EVENT_LABEL EVENT_CODES}

RUN_ROOT=${RUN_ROOT:-/home/fj/vllm_026_v2_kperf_runs}
MODEL=${MODEL:?set MODEL to the Qwen2.5-1.5B-Instruct directory}
SERVED_MODEL=${SERVED_MODEL:-qwen25-1_5b}
VLLM_BIN=${VLLM_BIN:-vllm}
VLLM_PYTHONPATH=${VLLM_PYTHONPATH:?set VLLM_PYTHONPATH to the mounted source root}
CPU_ID=${CPU_ID:-250}
GPU_ID=${GPU_ID:-0}
PORT=${PORT:-18026}

RUN_DIR="$RUN_ROOT/raw/$EVENT_LABEL"
if [[ -e "$RUN_DIR" ]]; then
    printf 'Run directory already exists: %s\n' "$RUN_DIR" >&2
    exit 3
fi
install -d -m 755 "$RUN_DIR"

SERVER_LOG="$RUN_DIR/server.log"
BENCH_LOG="$RUN_DIR/benchmark.log"
MEASUREMENT_LOG="$RUN_DIR/measurement.log"
SERVICE_PID=""

cleanup_service() {
    if [[ -z "$SERVICE_PID" ]]; then
        return
    fi
    if kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
        kill -TERM -- "-$SERVICE_PID" 2>/dev/null || true
        for _ in $(seq 1 30); do
            if ! kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
                break
            fi
            sleep 1
        done
    fi
    if kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
        kill -KILL -- "-$SERVICE_PID" 2>/dev/null || true
    fi
    wait "$SERVICE_PID" 2>/dev/null || true
}

trap cleanup_service EXIT INT TERM

if ss -ltn | grep -qE ":${PORT}[[:space:]]"; then
    printf 'Port %s is already in use\n' "$PORT" >&2
    exit 4
fi

nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader > "$RUN_DIR/gpu_before.txt"
nvidia-smi --query-compute-apps=pid,process_name,used_memory \
    --format=csv,noheader > "$RUN_DIR/gpu_processes_before.txt" || true

{
    printf 'version=0.26.0\n'
    printf 'model_runner=v2\n'
    printf 'event_label=%s\n' "$EVENT_LABEL"
    printf 'event_codes=%s\n' "$EVENT_CODES"
    printf 'kperf_time_mode=inner\n'
    printf 'cpu_id=%s\n' "$CPU_ID"
    printf 'gpu_id=%s\n' "$GPU_ID"
    printf 'port=%s\n' "$PORT"
    printf 'model=%s\n' "$MODEL"
    printf 'served_model=%s\n' "$SERVED_MODEL"
    printf 'vllm_bin=%s\n' "$VLLM_BIN"
    printf 'vllm_pythonpath=%s\n' "$VLLM_PYTHONPATH"
    printf 'input_tokens=7000\n'
    printf 'output_tokens=100\n'
    printf 'num_prompts=1\n'
    printf 'num_warmups=0\n'
    printf 'max_concurrency=1\n'
    printf 'request_rate=inf\n'
    printf 'temperature=0\n'
    printf 'seed=0\n'
    printf 'ignore_eos=true\n'
    printf 'max_model_len=8192\n'
    printf 'max_num_seqs=1\n'
    printf 'max_num_batched_tokens=8192\n'
    printf 'tensor_parallel_size=1\n'
    printf 'data_parallel_size=1\n'
    printf 'dtype=bfloat16\n'
    printf 'gpu_memory_utilization=0.9\n'
    printf 'enforce_eager=true\n'
    printf 'enable_prefix_caching=true\n'
    printf 'enable_chunked_prefill=true\n'
    printf 'start_time=%s\n' "$(date -Is)"
} > "$RUN_DIR/run.env"

setsid taskset -c "$CPU_ID" env \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="$VLLM_PYTHONPATH" \
    CUDA_VISIBLE_DEVICES="$GPU_ID" \
    VLLM_USE_V1=1 \
    VLLM_USE_V2_MODEL_RUNNER=1 \
    KPERF_RAW_EVENTS="$EVENT_CODES" \
    KPERF_EVENT_NAMES="$EVENT_CODES" \
    KPERF_TIME_MODE=inner \
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
    > "$SERVER_LOG" 2>&1 &
SERVICE_PID=$!
printf '%s\n' "$SERVICE_PID" > "$RUN_DIR/service.pid"

READY=0
for _ in $(seq 1 180); do
    if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
        break
    fi
    if curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null; then
        READY=1
        break
    fi
    sleep 2
done

if [[ "$READY" -ne 1 ]]; then
    printf 'Service did not become ready for %s\n' "$EVENT_LABEL" >&2
    tail -n 120 "$SERVER_LOG" >&2 || true
    exit 5
fi

sleep 2
MEASUREMENT_START_LINE=$(( $(wc -l < "$SERVER_LOG") + 1 ))
printf '%s\n' "$MEASUREMENT_START_LINE" > "$RUN_DIR/measurement_start_line.txt"
printf '%s\n' "$(date -Is)" > "$RUN_DIR/measurement_start_time.txt"

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
    > "$BENCH_LOG" 2>&1
BENCH_RC=$?
set -e

sleep 3
printf '%s\n' "$(date -Is)" > "$RUN_DIR/measurement_end_time.txt"
MEASUREMENT_END_LINE=$(wc -l < "$SERVER_LOG")
printf '%s\n' "$MEASUREMENT_END_LINE" > "$RUN_DIR/measurement_end_line.txt"

cleanup_service
SERVICE_PID=""
trap - EXIT INT TERM

if (( MEASUREMENT_END_LINE >= MEASUREMENT_START_LINE )); then
    sed -n "${MEASUREMENT_START_LINE},${MEASUREMENT_END_LINE}p" \
        "$SERVER_LOG" > "$MEASUREMENT_LOG"
else
    : > "$MEASUREMENT_LOG"
fi

nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu \
    --format=csv,noheader > "$RUN_DIR/gpu_after.txt"
nvidia-smi --query-compute-apps=pid,process_name,used_memory \
    --format=csv,noheader > "$RUN_DIR/gpu_processes_after.txt" || true
printf 'benchmark_exit_code=%s\n' "$BENCH_RC" >> "$RUN_DIR/run.env"
printf 'end_time=%s\n' "$(date -Is)" >> "$RUN_DIR/run.env"
sha256sum "$SERVER_LOG" "$BENCH_LOG" "$MEASUREMENT_LOG" \
    > "$RUN_DIR/SHA256SUMS.txt"

if [[ "$BENCH_RC" -ne 0 ]]; then
    printf 'Benchmark failed for %s with rc=%s\n' \
        "$EVENT_LABEL" "$BENCH_RC" >&2
    tail -n 120 "$BENCH_LOG" >&2 || true
    exit 6
fi

printf 'completed event=%s run_dir=%s start_line=%s end_line=%s\n' \
    "$EVENT_LABEL" "$RUN_DIR" "$MEASUREMENT_START_LINE" \
    "$MEASUREMENT_END_LINE"
