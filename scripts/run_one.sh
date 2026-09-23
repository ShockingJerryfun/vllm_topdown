#!/usr/bin/env bash

set -Eeuo pipefail

LABEL=${1:?usage: run_one.sh LABEL [EVENT_CODES] [EVENT_NAMES] [PMU_SCOPE]}
CODES=${2-}
NAMES=${3:-$CODES}
PMU_SCOPE=${4:-thread}
KPERF_TARGET=${KPERF_TARGET:-}
KPERF_QUALIFIER=${KPERF_QUALIFIER:-}
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
SESSION_DIR=${KPERF_SESSION_DIR:-}
SERVICE_LOG="$RUN_DIR/server.log"
SERVICE_PID=""
PERF_PID=""
FREQUENCY_PID=""
BENCH_PID=""
BENCH_SERVICE_PID=""

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
    trap '' INT TERM
    if [[ -n "$FREQUENCY_PID" ]]; then
        kill -TERM "$FREQUENCY_PID" 2>/dev/null || true
        wait "$FREQUENCY_PID" 2>/dev/null || true
    fi
    if [[ -n "$BENCH_SERVICE_PID" ]] \
        && [[ -z "$SESSION_DIR" || ! -s "$SESSION_DIR/benchmark_client/ready.json" ]]; then
        kill -TERM "$BENCH_SERVICE_PID" 2>/dev/null || true
        wait "$BENCH_SERVICE_PID" 2>/dev/null || true
    fi
    if [[ -n "$BENCH_PID" ]]; then
        kill -TERM -- "-$BENCH_PID" 2>/dev/null || true
        for _ in $(seq 1 25); do
            kill -0 -- "-$BENCH_PID" 2>/dev/null || break
            sleep 0.2
        done
        kill -KILL -- "-$BENCH_PID" 2>/dev/null || true
        wait "$BENCH_PID" 2>/dev/null || true
    fi
    if [[ -n "$PERF_PID" ]] && kill -0 "$PERF_PID" 2>/dev/null; then
        kill -INT "$PERF_PID" 2>/dev/null || true
        for _ in $(seq 1 40); do
            kill -0 "$PERF_PID" 2>/dev/null || break
            sleep 1
        done
        kill -TERM "$PERF_PID" 2>/dev/null || true
        sleep 1
        kill -KILL "$PERF_PID" 2>/dev/null || true
        wait "$PERF_PID" 2>/dev/null || true
    fi
    if [[ -n "$SERVICE_PID" ]] && kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
        # Let the API/EngineCore reap their children before group-wide fallback.
        kill -TERM "$SERVICE_PID" 2>/dev/null || true
        for _ in $(seq 1 "$SHUTDOWN_ATTEMPTS"); do
            kill -0 -- "-$SERVICE_PID" 2>/dev/null || break
            sleep "$SHUTDOWN_INTERVAL"
        done
        if kill -0 -- "-$SERVICE_PID" 2>/dev/null; then
            kill -TERM -- "-$SERVICE_PID" 2>/dev/null || true
            sleep 1
            kill -KILL -- "-$SERVICE_PID" 2>/dev/null || true
        fi
        wait "$SERVICE_PID" 2>/dev/null || true
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ ! -e "$RUN_DIR" ]] || { printf 'Exists: %s\n' "$RUN_DIR" >&2; exit 2; }
install -d -m 755 "$RUN_DIR"
cat /proc/sys/kernel/random/boot_id > "$RUN_DIR/boot_id"
if [[ -n ${TOPDOWN_SUPERVISOR_COMMAND:-} ]]; then
    cp "$TOPDOWN_SUPERVISOR_COMMAND" "$RUN_DIR/supervisor_command.json"
fi

source "$SCRIPT_DIR/placement.sh"
if [[ -n ${CODE_PAGE_CONDITION:-} ]]; then
    source "$SCRIPT_DIR/pages/evidence.sh"
else
    page_snapshot() { return 0; }
fi
if [[ "$PLACEMENT_MODE" == worker_set ]]; then
    export KPERF_RUNTIME_IDENTITY_DIR="$RUN_DIR/runtime_identity"
fi

COLLECT_MODE=disabled
KPERF_ENV=(KPERF_ENABLE=0)
if [[ "$LABEL" == service ]]; then
    KPERF_ENV=(KPERF_ENABLE=0 KPERF_CONTROL=1 VLLM_SERVER_DEV_MODE=1)
elif [[ "$LABEL" == time ]]; then
    COLLECT_MODE=time
    KPERF_ENV=(
        KPERF_ENABLE=1
        KPERF_MODE=time
        KPERF_TARGET="$KPERF_TARGET"
        KPERF_QUALIFIER="$KPERF_QUALIFIER"
    )
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
        KPERF_TARGET="$KPERF_TARGET"
        KPERF_QUALIFIER="$KPERF_QUALIFIER"
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
    printf 'target=%s\n' "$KPERF_TARGET"
    printf 'qualifier=%s\n' "$KPERF_QUALIFIER"
    printf 'model=%s\n' "$MODEL"
    printf 'source=%s\n' "$SOURCE_ROOT"
    printf 'execution_mode=%s\n' "$EXECUTION_MODE"
    printf 'server_flags=%s\n' "$SERVER_FLAGS"
    printf 'placement_mode=%s\n' "$PLACEMENT_MODE"
    printf 'hotspot_scope=%s\n' "${HOTSPOT_SCOPE:-legacy}"
    printf 'worker_cpus=%s\nworker_pool_cpus=%s\nworker_numa_node=%s\nservice_cpus=%s\nclient_cpus=%s\n' \
        "${WORKER_CPUS:-}" "${WORKER_POOL_CPUS:-}" "${WORKER_NUMA_NODE:-}" "${SERVICE_CPUS:-}" "${CLIENT_CPUS:-}"
    printf 'service_session=%s\n' "$SESSION_DIR"
} > "$RUN_DIR/run.env"

read -r -a EXTRA_SERVER_FLAGS <<< "$SERVER_FLAGS"
read -r -a PREFIX_CACHING_ARGS <<< "$PREFIX_CACHING_FLAG"
read -r -a IGNORE_EOS_ARGS <<< "$IGNORE_EOS_FLAG"
read -r -a PERF_REPORT_ARGS <<< "$PERF_REPORT_FLAGS"

SERVER_COMMAND=(
    setsid "${SERVER_PREFIX[@]}" env
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
    "${BINDING_ARGS[@]}"
)
if [[ -z "$SESSION_DIR" || "$LABEL" == service ]]; then
    if [[ "$LABEL" == service ]]; then
        [[ "$SERVER_HOST" == 127.0.0.1 ]] || {
            printf 'Runtime PMU control requires SERVER_HOST=127.0.0.1\n' >&2
            exit 4
        }
        if curl -fsS "http://${SERVER_HOST}:${PORT}${HEALTH_ENDPOINT}" >/dev/null 2>&1; then
            printf 'Port %s already serves a running instance\n' "$PORT" >&2
            exit 4
        fi
    fi
    record_command "$LABEL vLLM serve" "$SERVICE_LOG" 1 1 "${SERVER_COMMAND[@]}"
    "${SERVER_COMMAND[@]}" > "$SERVICE_LOG" 2>&1 &
    SERVICE_PID=$!
    READY=0
    for _ in $(seq 1 "$READY_CHECK_ATTEMPTS"); do
    [[ -z ${SPE_RUN:-} || ! -e "$SPE_RUN/gates/abort" ]] || exit 1
        kill -0 "$SERVICE_PID" 2>/dev/null || break
        if curl -fsS "http://${SERVER_HOST}:${PORT}${HEALTH_ENDPOINT}" >/dev/null 2>&1; then
            READY=1
            break
        fi
        sleep "$READY_CHECK_INTERVAL"
    done
    [[ "$READY" -eq 1 ]] || { tail -n 120 "$SERVICE_LOG" >&2; exit 4; }
    BIND_API_PID=$SERVICE_PID
    BIND_IDENTITY="$RUN_DIR/placement_identity.json"
    placement_check bind
    if [[ "$LABEL" == service ]]; then
        printf '%s\n' "$SERVICE_PID" > "$RUN_DIR/ready"
        wait "$SERVICE_PID"
        exit $?
    fi
else
    SERVICE_LOG="$SESSION_DIR/server.log"
    for _ in $(seq 1 "$READY_CHECK_ATTEMPTS"); do
    [[ -z ${SPE_RUN:-} || ! -e "$SPE_RUN/gates/abort" ]] || exit 1
        kill -0 "$KPERF_SERVICE_RUNNER_PID" 2>/dev/null || exit 4
        [[ -s "$SESSION_DIR/ready" ]] && break
        sleep "$READY_CHECK_INTERVAL"
    done
    [[ -s "$SESSION_DIR/ready" ]] || exit 4
    BIND_API_PID=$(cat "$SESSION_DIR/ready")
    BIND_IDENTITY="$SESSION_DIR/placement_identity.json"
fi
placement_check check

[[ -n "$SESSION_DIR" ]] || sleep "$SERVICE_SETTLE_SECONDS"
START_LINE=$(( $(wc -l < "$SERVICE_LOG") + 1 ))
ROUND_START_LINE=$START_LINE
if [[ -n "$SESSION_DIR" ]]; then
    SWITCH_COMMAND=(
        env "${KPERF_ENV[@]}" KPERF_MODE="$COLLECT_MODE"
        "$PYTHON_BIN" "$SCRIPT_DIR/switch_pmu.py"
        "http://${SERVER_HOST}:${PORT}" "$RUN_DIR" "$SESSION_DIR/worker.json"
        --timeout "$(( READY_CHECK_ATTEMPTS * READY_CHECK_INTERVAL ))"
    )
    record_command "$LABEL switch PMU" "$RUN_DIR/switch.json" 1 0 "${SWITCH_COMMAND[@]}"
    "${SWITCH_COMMAND[@]}" > "$RUN_DIR/switch.json"
    if [[ "$PLACEMENT_MODE" == worker_set ]]; then
        "$PYTHON_BIN" -c 'import json,sys; a=json.load(open(sys.argv[1])); b=json.load(open(sys.argv[2])); assert a["pid"] == a["tid"] == b["worker"], "RPC Worker identity mismatch"' \
            "$SESSION_DIR/worker.json" "$BIND_IDENTITY"
    fi
    placement_check check
fi

if [[ "$LABEL" == hotspot ]]; then
    if [[ -n "$SESSION_DIR" ]]; then
        WORKER_PID=$("$PYTHON_BIN" -c \
            'import json, sys; print(json.load(open(sys.argv[1]))["tid"])' \
            "$SESSION_DIR/worker.json")
        PERF_TARGET=(-t "$WORKER_PID")
    else
        WORKER_PID=$("$PYTHON_BIN" "$SCRIPT_DIR/placement.py" worker --api "$BIND_API_PID")
        PERF_TARGET=(-p "$WORKER_PID")
    fi
    case "${HOTSPOT_SCOPE:-legacy}" in
        thread) PERF_TARGET=(-t "$WORKER_PID") ;;
        worker)
            WORKER_PID=$("$PYTHON_BIN" "$SCRIPT_DIR/placement.py" worker --api "$BIND_API_PID")
            PERF_TARGET=(-p "$WORKER_PID") ;;
        legacy) ;;
        *) exit 2 ;;
    esac
    if [[ "${HOTSPOT_SCOPE:-legacy}" == thread ]]; then
        PERF_TARGET+=(--no-inherit)
    fi
    [[ -n "$WORKER_PID" ]] || {
        printf 'No process matched: %s\n' "$HOTSPOT_WORKER_PATTERN" >&2
        exit 8
    }
    PERF_COMMAND=(
        perf record
        -e "$PERF_EVENT"
        -c "$PERF_PERIOD"
        -o "$RUN_DIR/perf.data"
        "${PERF_TARGET[@]}"
    )
    record_command "$LABEL perf record" "$RUN_DIR/perf.log" 1 1 \
        "PYTHONPERFSUPPORT=$PYTHON_PERF_SUPPORT" "${PERF_COMMAND[@]}"
    PYTHONPERFSUPPORT="$PYTHON_PERF_SUPPORT" \
        "${PERF_COMMAND[@]}" > "$RUN_DIR/perf.log" 2>&1 &
    PERF_PID=$!
    sleep "$PERF_SETTLE_SECONDS"
fi

BENCHMARK_ARGS=(
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
BENCHMARK_COMMAND=(
    setsid "${CLIENT_PREFIX[@]}" env
    PYTHONPATH="$VLLM_PYTHONPATH"
    CUDA_VISIBLE_DEVICES="$GPU_ID"
    VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER"
    "$VLLM_BIN" bench serve
    "${BENCHMARK_ARGS[@]}"
)
CLIENT_STATE="${SESSION_DIR:-$RUN_DIR}/benchmark_client"
if [[ ${PERSISTENT_BENCH_CLIENT:-1} == 1 ]]; then
    install -d -m 755 "$CLIENT_STATE"
    if [[ ! -s "$CLIENT_STATE/ready.json" ]]; then
        setsid "${CLIENT_PREFIX[@]}" env PYTHONPATH="$VLLM_PYTHONPATH" \
            CUDA_VISIBLE_DEVICES="$GPU_ID" VLLM_USE_V2_MODEL_RUNNER="$VLLM_USE_V2_MODEL_RUNNER" \
            "$PYTHON_BIN" "$SCRIPT_DIR/bench_client.py" serve --state "$CLIENT_STATE" \
            "${BENCHMARK_ARGS[@]}" > "$CLIENT_STATE/server.log" 2>&1 &
        BENCH_SERVICE_PID=$!
        printf '%s\n' "$BENCH_SERVICE_PID" > "$CLIENT_STATE/pid"
        cat "/proc/$BENCH_SERVICE_PID/stat" > "$CLIENT_STATE/process.stat"
        for _ in $(seq 1 "$READY_CHECK_ATTEMPTS"); do
            kill -0 "$BENCH_SERVICE_PID" 2>/dev/null || { cat "$CLIENT_STATE/server.log" >&2; exit 5; }
            [[ -s "$CLIENT_STATE/ready.json" ]] && break
            sleep "$READY_CHECK_INTERVAL"
        done
        [[ -s "$CLIENT_STATE/ready.json" ]] || exit 5
    fi
    BENCHMARK_COMMAND=(setsid "$PYTHON_BIN" "$SCRIPT_DIR/bench_client.py" request
        --state "$CLIENT_STATE" --log "$RUN_DIR/benchmark.log")
fi
record_command "$LABEL vLLM benchmark" "$RUN_DIR/benchmark.log" 1 0 \
    "${BENCHMARK_COMMAND[@]}"
run_benchmark() {
    local log=$1
    placement_check check
    if [[ ${PERSISTENT_BENCH_CLIENT:-1} == 1 ]]; then
        setsid "$PYTHON_BIN" "$SCRIPT_DIR/bench_client.py" request \
            --state "$CLIENT_STATE" --log "$log" > "$log.ipc" 2>&1 &
    else
        "${BENCHMARK_COMMAND[@]}" > "$log" 2>&1 &
    fi
    BENCH_PID=$!
    while kill -0 "$BENCH_PID" 2>/dev/null; do
        [[ -z ${SPE_RUN:-} || ! -e "$SPE_RUN/gates/abort" ]] || exit 1
        placement_check check
        sleep 0.2
    done
    BENCH_RC=0
    wait "$BENCH_PID" || BENCH_RC=$?
    BENCH_PID=""
    placement_check check
}
if [[ "$LABEL" == spe ]]; then
    source "$SCRIPT_DIR/spe/requests.sh"
else
    source "$SCRIPT_DIR/cooling.sh"
    cool_before_group
    source "$SCRIPT_DIR/warmup.sh"
    run_round_warmups
    page_snapshot before
    START_LINE=$(( $(wc -l < "${SERVICE_LOG:-$RUN_DIR/server.log}") + 1 ))
    if [[ "$LABEL" == frequency ]]; then
        "$PYTHON_BIN" "$SCRIPT_DIR/frequency.py" --devkit "${DEVKIT_BIN:?set DEVKIT_BIN}" \
            --output "$RUN_DIR" --cpus "$WORKER_CPUS" --node "$WORKER_NUMA_NODE" \
            > "$RUN_DIR/collector.log" 2>&1 &
        FREQUENCY_PID=$!
        for _ in $(seq 1 350); do
            [[ ! -e "$RUN_DIR/frequency.ready" ]] || break
            kill -0 "$FREQUENCY_PID" 2>/dev/null || { cat "$RUN_DIR/collector.log" >&2; exit 1; }
            sleep 0.1
        done
        [[ -e "$RUN_DIR/frequency.ready" ]] || exit 1
    fi
    run_benchmark "$RUN_DIR/benchmark.log"
    if [[ "$LABEL" == frequency ]]; then
        touch "$RUN_DIR/frequency.stop"
        wait "$FREQUENCY_PID"
        FREQUENCY_PID=""
    fi
    page_snapshot after
fi

if [[ -n "$PERF_PID" ]]; then
    kill -INT "$PERF_PID" 2>/dev/null || true
    wait "$PERF_PID" 2>/dev/null || true
    PERF_PID=""
fi

[[ -n "$SESSION_DIR" ]] || sleep "$SERVICE_SETTLE_SECONDS"
if [[ -n "$SESSION_DIR" ]]; then
    record_command "$LABEL stop PMU" "$RUN_DIR/stop.json" 1 0 "${SWITCH_COMMAND[@]}" --stop
    "${SWITCH_COMMAND[@]}" --stop > "$RUN_DIR/stop.json"
fi
END_LINE=$(wc -l < "$SERVICE_LOG")
cleanup
SERVICE_PID=""
trap - EXIT INT TERM
sed -n "${START_LINE},${END_LINE}p" "$SERVICE_LOG" > "$RUN_DIR/measurement.log"
if [[ -n "$SESSION_DIR" ]]; then
    sed -n "${ROUND_START_LINE},${END_LINE}p" "$SERVICE_LOG" > "$RUN_DIR/server.log"
fi

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

if [[ ${RESUME_COLLECTION:-0} == 1 ]]; then
    "$PYTHON_BIN" "$SCRIPT_DIR/resume.py" commit "$RUN_DIR"
fi
printf 'completed %s\n' "$LABEL"
