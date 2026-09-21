#!/usr/bin/env bash
# Sourced only after config.env; defaults preserve older copied configurations.
PLACEMENT_MODE=${PLACEMENT_MODE:-legacy}
SERVER_PREFIX=()
CLIENT_PREFIX=()
BINDING_ARGS=()
if [[ "$PLACEMENT_MODE" == worker_set ]]; then
    [[ "$TENSOR_PARALLEL_SIZE" == 1 && "$DATA_PARALLEL_SIZE" == 1 ]] || {
        printf 'Explicit placement currently requires TP=DP=1\n' >&2; exit 2;
    }
    [[ "$SERVER_FLAGS" != *--numa-* && "$SERVER_FLAGS" != *--distributed-executor-backend* ]] || {
        printf 'Set placement through CPU variables, not SERVER_FLAGS\n' >&2; exit 2;
    }
    "$PYTHON_BIN" "$SCRIPT_DIR/placement.py" preflight > "$RUN_DIR/placement_config.json"
    numactl --physcpubind="$WORKER_CPUS" --membind="$WORKER_NUMA_NODE" true
    export VLLM_WORKER_MULTIPROC_METHOD=spawn KPERF_STRICT_NUMA=1
    # Keep the parent allowed set broad until numactl has spawned the Worker.
    # API/EngineCore are moved only after readiness, before benchmark warmup.
    CLIENT_PREFIX=(taskset -c "$CLIENT_CPUS")
    BINDING_ARGS=(--distributed-executor-backend mp --numa-bind
        --numa-bind-nodes "$WORKER_NUMA_NODE" --numa-bind-cpus "$WORKER_CPUS")
elif [[ "$PLACEMENT_MODE" != legacy ]]; then
    printf 'Invalid PLACEMENT_MODE=%s\n' "$PLACEMENT_MODE" >&2; exit 2
fi

placement_check() {
    [[ "$PLACEMENT_MODE" == worker_set ]] || return 0
    # Keep the auditing shell and later helpers off the reserved Worker pool.
    taskset -pc "$SERVICE_CPUS" "$$" >/dev/null
    taskset -c "$SERVICE_CPUS" "$PYTHON_BIN" "$SCRIPT_DIR/placement.py" "$1" --api "$BIND_API_PID" \
        --identity "$BIND_IDENTITY" >> "$RUN_DIR/placement_checks.jsonl"
}
