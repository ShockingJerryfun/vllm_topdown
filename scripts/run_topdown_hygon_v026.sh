#!/usr/bin/env bash

set -Eeuo pipefail

CONTAINER=${CONTAINER:-vllm026_qwen}
PROJECT=${PROJECT:-/home/f00955680/vllm_fj}
MODEL=${MODEL:-/home/f00955680/models/Qwen3-8B}
VLLM_BIN=${VLLM_BIN:-vllm}
GPU_ID=${GPU_ID:-0}
RUN_ID=${RUN_ID:-$(date '+%Y%m%d_%H%M%S')}
RUN_ROOT="$PROJECT/results/hygon_c86_7490/$RUN_ID"
RUNNER="$PROJECT/scripts/hygon_c86_7490/run.sh"

printf '[hygon] container=%s output=%s\n' "$CONTAINER" "$RUN_ROOT"
docker inspect "$CONTAINER" >/dev/null
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == true ]] ||
    docker start "$CONTAINER" >/dev/null
docker exec "$CONTAINER" test -f "$RUNNER"
docker exec "$CONTAINER" test -d "$MODEL"

docker exec \
    -e RUN_ROOT="$RUN_ROOT" \
    -e MODEL="$MODEL" \
    -e VLLM_BIN="$VLLM_BIN" \
    -e GPU_ID="$GPU_ID" \
    -e SERVER_FLAGS="${SERVER_FLAGS:-}" \
    -e MAX_MODEL_LEN="${MAX_MODEL_LEN:-16384}" \
    -e MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS:-163840}" \
    -e GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.8}" \
    -e RANDOM_INPUT_LEN="${RANDOM_INPUT_LEN:-7000}" \
    -e RANDOM_OUTPUT_LEN="${RANDOM_OUTPUT_LEN:-100}" \
    -e REQUEST_RATE="${REQUEST_RATE:-1}" \
    -e NUM_PROMPTS="${NUM_PROMPTS:-1}" \
    "$CONTAINER" bash "$RUNNER"

test -s "$RUN_ROOT/summary.csv"
test -s "$RUN_ROOT/collection_quality.csv"
test -s "$RUN_ROOT/hotspot/perf_report.txt"
printf '[hygon] completed: %s\n' "$RUN_ROOT"
