#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUN_ROOT=${RUN_ROOT:-/home/fj/vllm_026_v2_kperf_runs}
RUN_TAG=${RUN_TAG:-$(date +%Y%m%d_%H%M%S)}

run_and_parse() {
    local group=$1
    local codes=$2
    local label="${RUN_TAG}_${group}"

    RUN_ROOT="$RUN_ROOT" "$SCRIPT_DIR/run_one.sh" "$label" "$codes"
    python3 "$SCRIPT_DIR/parse_run.py" \
        "$RUN_ROOT/raw/$label" \
        --version 0.26.0 \
        --event-label "$group" \
        --event-codes "$codes"
}

run_and_parse TOPDOWN 0x0011,0x0008,0x003e,0x001b
run_and_parse ICACHE 0x0008,0x0001,0x0014,0x0027,0x0028
run_and_parse IMIX 0x001b,0x0070,0x0073,0x8005,0x0078
run_and_parse IMIX2 0x001b,0x0071,0x0079,0x007a,0x0075,0x8006
