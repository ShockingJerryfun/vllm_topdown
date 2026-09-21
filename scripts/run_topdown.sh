#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ $# -le 2 ]] || { printf 'usage: run_topdown.sh [ABSOLUTE_CONFIG [RUN_ROOT]]\n' >&2; exit 2; }
[[ $# -eq 0 ]] || export TOPDOWN_CONFIG=$1
set -a
source "$SCRIPT_DIR/config.env"
set +a

COLLECTION_PROFILE=${COLLECTION_PROFILE:-full}
case "$COLLECTION_PROFILE" in full|end_to_end) ;; *) exit 2 ;; esac
if [[ "$COLLECTION_PROFILE" == end_to_end && "$CHIP" != 920b && "$CHIP" != 950 ]]; then
    printf 'end_to_end currently supports 920b and 950\n' >&2
    exit 2
fi
RUN_ID=$(date '+%Y%m%d_%H%M%S')
RUN_ROOT=${2:-$PROJECT/results/$CHIP/$RUN_ID}
RUNNER="$PROJECT/scripts/$CHIP/run.sh"

if [[ "$SPE_ENABLE" == auto ]]; then
    SPE_ENABLE=0
    if [[ "$PLACEMENT_MODE" == worker_set && ( "$CHIP" == 920b || "$CHIP" == 950 ) ]]; then
        SPE_ENABLE=1
    fi
fi
case "$SPE_ENABLE" in 0|1) ;; *) printf 'SPE_ENABLE must be auto, 0 or 1\n' >&2; exit 2 ;; esac
if [[ "$SPE_ENABLE" == 1 ]]; then
    [[ "$CHIP" == 920b || "$CHIP" == 950 ]] || { printf 'SPE requires an Arm SPE chip\n' >&2; exit 2; }
    [[ "$PLACEMENT_MODE" == worker_set ]] || { printf 'SPE requires explicit Worker placement\n' >&2; exit 2; }
    : "${TOPDOWN_CONFIG:?set a task configuration for SPE}"
    : "${HOST_PYTHON:?set HOST_PYTHON}"
    : "${EXPERIMENT_LOCK:?set EXPERIMENT_LOCK}"
    : "${SUBREAPER_BIN:?set SUBREAPER_BIN}"
    : "${SPE_BINARY_CACHE:?set SPE_BINARY_CACHE}"
fi

printf '[%s] container=%s output=%s\n' "$CHIP" "$CONTAINER" "$RUN_ROOT"
docker inspect "$CONTAINER" >/dev/null
[[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" == true ]] ||
    docker start "$CONTAINER" >/dev/null
docker exec "$CONTAINER" test -f "$RUNNER"
docker exec "$CONTAINER" test -d "$MODEL"

if [[ "$SPE_ENABLE" == 1 ]]; then
    export SPE_ENABLE COLLECTION_PROFILE
    exec bash "$SCRIPT_DIR/run_experiment.sh" "$TOPDOWN_CONFIG" "$RUN_ROOT"
fi

docker exec \
    -e SPE_ENABLE=0 \
    -e TOPDOWN_CONFIG \
    -e COLLECTION_PROFILE -e PLACEMENT_MODE -e WORKER_CPUS -e WORKER_POOL_CPUS \
    -e WORKER_NUMA_NODE -e SERVICE_CPUS -e CLIENT_CPUS -e HOTSPOT_SCOPE \
    -e RUN_ROOT="$RUN_ROOT" \
    "$CONTAINER" bash "$RUNNER"

test -s "$RUN_ROOT/summary.csv"
test -s "$RUN_ROOT/collection_quality.csv"
if [[ "$COLLECTION_PROFILE" == full ]]; then
    test -s "$RUN_ROOT/hotspot/perf_report.txt"
fi
test -s "$RUN_ROOT/commands.txt"
REPORT=$(find "$RUN_ROOT" -maxdepth 1 -type f -name '*.xlsx' -print -quit)
test -n "$REPORT"
test -s "$REPORT"
printf '[%s] completed: %s\nreport: %s\n' "$CHIP" "$RUN_ROOT" "$REPORT"
