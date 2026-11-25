#!/usr/bin/env bash
set -Eeuo pipefail
SPE_TOOLS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SPE_TOOLS")
SOURCE_ROOT=$(dirname "$COMMON_DIR")
set -a
source "$COMMON_DIR/config.env"
set +a
: "${SPE_RUN:?set SPE_RUN}"
[[ "$PLACEMENT_MODE" == worker_set ]] || { printf 'SPE requires explicit Worker placement\n' >&2; exit 2; }
RUNTIME=$(mktemp -d /tmp/vllm_spe.XXXXXX)
cleanup_runtime() { rm -rf -- "$RUNTIME"; }
trap cleanup_runtime EXIT
if [[ -z "$VLLM_SITE" ]]; then
    VLLM_SITE=$("$PYTHON_BIN" -c 'import importlib.util; print(next(iter(importlib.util.find_spec("vllm").submodule_search_locations)))')
fi
cp -rs "$VLLM_SITE" "$RUNTIME/vllm"
while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_ROOT/vllm/"}
    target="$RUNTIME/vllm/$relative"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$file" "$target"
done < <(find "$SOURCE_ROOT/vllm" -type f ! -name '*.pyc' -print0)
ln -s "$SOURCE_ROOT/kperf_instrument.py" "$RUNTIME/kperf_instrument.py"
"$PYTHON_BIN" "$SPE_TOOLS/prepare.py" "$RUNTIME" "$SPE_RUN"
export SOURCE_ROOT VLLM_PYTHONPATH=$RUNTIME
export SPE_EXPECTED_CALLS=$(( RANDOM_OUTPUT_LEN - 1 ))
unset KPERF_SESSION_DIR KPERF_SERVICE_RUNNER_PID
RUN_ROOT="$SPE_RUN/runs" bash "$COMMON_DIR/run_one.sh" spe
