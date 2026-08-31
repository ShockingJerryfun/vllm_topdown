#!/usr/bin/env bash

set -Eeuo pipefail

CHIP=${CHIP:?set CHIP to a directory under scripts}
CONTAINER=${CONTAINER:-qwen3_container_fj}
PROJECT=${PROJECT:-/home/fj/vllm_topdown}
MODEL=${MODEL:-/home/model/Qwen3-8B-Instruct}
VLLM_BIN=${VLLM_BIN:-vllm}
GPU_ID=${GPU_ID:-0}
RUN_ID=${RUN_ID:-$(date '+%Y%m%d_%H%M%S')}
RUN_ROOT="$PROJECT/results/$CHIP/$RUN_ID"
RUNNER="$PROJECT/scripts/$CHIP/run.sh"

printf '[%s] container=%s output=%s\n' "$CHIP" "$CONTAINER" "$RUN_ROOT"
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
    -e MODEL_SHORT="${MODEL_SHORT:-qwen3}" \
    -e VLLM_VERSION_SHORT="${VLLM_VERSION_SHORT:-0.26}" \
    -e SERVER_FLAGS="${SERVER_FLAGS:---async-scheduling}" \
    -e MAX_MODEL_LEN="${MAX_MODEL_LEN:-8192}" \
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
REPORT=$(find "$RUN_ROOT" -maxdepth 1 -type f -name '*.xlsx' -print -quit)
test -n "$REPORT"
test -s "$REPORT"
printf '[%s] completed: %s\nreport: %s\n' "$CHIP" "$RUN_ROOT" "$REPORT"
