#!/usr/bin/env bash
# Installed as scripts/run_experiment.sh in each maintained collector.
set -Eeuo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export TOPDOWN_CONFIG=${1:?usage: run_experiment.sh ABSOLUTE_CONFIG [RUN_ROOT]}
export COLLECTION_PROFILE=${COLLECTION_PROFILE:-end_to_end}
export SPE_ENABLE=${SPE_ENABLE:-1}
set -a
source "$SCRIPT_DIR/config.env"
set +a
[[ "$PLACEMENT_MODE" == worker_set ]] || { printf 'Set explicit Worker placement in the configuration\n' >&2; exit 2; }
RUN_ROOT=${2:-$PROJECT/results/$CHIP/$(date '+%Y%m%d_%H%M%S')}
: "${HOST_PYTHON:?set HOST_PYTHON to the host task virtual environment}"
: "${EXPERIMENT_LOCK:?set a shared experiment lock path}"
: "${SUBREAPER_BIN:?set SUBREAPER_BIN to the task copy of docker-init or tini}"
: "${SPE_BINARY_CACHE:?set a retained binary evidence directory}"
COMMAND=("$HOST_PYTHON" "$SCRIPT_DIR/spe/supervise.py"
    --project "$PROJECT" --run "$RUN_ROOT" --config "$TOPDOWN_CONFIG"
    --container "$CONTAINER" --python "$PYTHON_BIN" --chip "$CHIP" --reaper "$SUBREAPER_BIN"
    --profile "$COLLECTION_PROFILE" --cpus "$WORKER_CPUS" --pool "$WORKER_POOL_CPUS"
    --service "$SERVICE_CPUS" --client "$CLIENT_CPUS" --node "$WORKER_NUMA_NODE" --gpu "$GPU_ID"
    --binary-cache "$SPE_BINARY_CACHE" --lock "$EXPERIMENT_LOCK"
    --max-temperature "${GPU_THERMAL_LIMIT:-85}")
[[ ${RESUME_COLLECTION:-0} != 1 ]] || COMMAND+=(--resume)
[[ -z ${CODE_PAGE_CONDITION:-} ]] || COMMAND+=(--pages "$CODE_PAGE_CONDITION")
[[ ${CODE_PAGE_AUDIT_MODE:-strict} != observe ]] || COMMAND+=(--observe-pages)
[[ "$SPE_ENABLE" == 0 ]] || COMMAND+=(--spe)
exec "${COMMAND[@]}"
