#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
set -a
source "$SCRIPT_DIR/config.env"
set +a

RUN_ID=$(date '+%Y%m%d_%H%M%S')
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
    "$CONTAINER" bash "$RUNNER"

test -s "$RUN_ROOT/summary.csv"
test -s "$RUN_ROOT/collection_quality.csv"
test -s "$RUN_ROOT/hotspot/perf_report.txt"
test -s "$RUN_ROOT/commands.txt"
REPORT=$(find "$RUN_ROOT" -maxdepth 1 -type f -name '*.xlsx' -print -quit)
test -n "$REPORT"
test -s "$REPORT"
printf '[%s] completed: %s\nreport: %s\n' "$CHIP" "$RUN_ROOT" "$REPORT"
