#!/usr/bin/env bash

# Sourced by chip runners after creating their temporary runtime overlay.
SERVICE_RUNNER_PID=""

stop_session() {
    if [[ -s ${KPERF_SESSION_DIR:-}/benchmark_client/ready.json ]]; then
        local client_pid
        client_pid=$(cat "$KPERF_SESSION_DIR/benchmark_client/pid")
        kill -TERM "$client_pid" 2>/dev/null || true
        for _ in $(seq 1 50); do
            [[ ! -e "$KPERF_SESSION_DIR/benchmark_client/ready.json" ]] && break
            sleep 0.1
        done
    fi
    if [[ -n "$SERVICE_RUNNER_PID" ]]; then
        kill -TERM "$SERVICE_RUNNER_PID" 2>/dev/null || true
        wait "$SERVICE_RUNNER_PID" 2>/dev/null || true
        SERVICE_RUNNER_PID=""
    fi
}

cleanup_session() {
    trap '' INT TERM
    stop_session
    rm -rf -- "$RUNTIME"
}

start_session() {
    export KPERF_SESSION_DIR="$RUN_ROOT/service"
    local session_root="$RUN_ROOT"
    if [[ ${RESUME_COLLECTION:-0} == 1 && -e "$KPERF_SESSION_DIR" ]]; then
        local number=1
        while [[ -e "$RUN_ROOT/.sessions/$number" ]]; do ((number+=1)); done
        session_root="$RUN_ROOT/.sessions/$number"
        mkdir -p "$session_root"
        export KPERF_SESSION_DIR="$session_root/service"
    fi
    RUN_ROOT="$session_root" bash "$COMMON_DIR/run_one.sh" service &
    SERVICE_RUNNER_PID=$!
    export KPERF_SERVICE_RUNNER_PID=$SERVICE_RUNNER_PID
}

trap cleanup_session EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
