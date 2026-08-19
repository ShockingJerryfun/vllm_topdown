#!/usr/bin/env bash

set -Eeuo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON_DIR=$(dirname "$SCRIPT_DIR")
SOURCE_ROOT=$(dirname "$COMMON_DIR")
RUN_ROOT=${RUN_ROOT:-$SOURCE_ROOT/results/920b}
PYTHON_BIN=${PYTHON_BIN:-$(dirname "${VLLM_BIN:-vllm}")/python}
[[ -x "$PYTHON_BIN" ]] || PYTHON_BIN=python3
[[ ! -e "$RUN_ROOT" ]] || { printf 'Exists: %s\n' "$RUN_ROOT" >&2; exit 2; }
install -d -m 755 "$RUN_ROOT"
VLLM_SITE=${VLLM_SITE:-$("$PYTHON_BIN" -c 'import importlib.util; print(next(iter(importlib.util.find_spec("vllm").submodule_search_locations)))')}
RUNTIME=$(mktemp -d /tmp/vllm.XXXXXX)
trap 'rm -rf -- "$RUNTIME"' EXIT
cp -rs "$VLLM_SITE" "$RUNTIME/vllm"
while IFS= read -r -d '' file; do
    relative=${file#"$SOURCE_ROOT/vllm/"}
    target="$RUNTIME/vllm/$relative"
    mkdir -p "$(dirname "$target")"
    ln -sfn "$file" "$target"
done < <(find "$SOURCE_ROOT/vllm" -type f -print0)
ln -s "$SOURCE_ROOT/kperf_instrument.py" "$RUNTIME/kperf_instrument.py"
export SOURCE_ROOT VLLM_PYTHONPATH=$RUNTIME

PYTHONPATH="$VLLM_PYTHONPATH" VLLM_USE_V2_MODEL_RUNNER=1 \
    "$PYTHON_BIN" -c \
    'import os, sys, torch, vllm; from vllm.v1.worker.gpu import model_runner; from vllm.model_executor.models import qwen3; print(sys.version); print(vllm.__version__); print(torch.__version__); print(os.path.realpath(vllm.__file__)); print(os.path.realpath(model_runner.__file__)); print(os.path.realpath(qwen3.__file__))' \
    > "$RUN_ROOT/runtime.txt"

while IFS='|' read -r label codes; do
    RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" "$label" "$codes" "$codes"
    "$PYTHON_BIN" "$COMMON_DIR/parse_run.py" "$RUN_ROOT/$label" \
        --event-names "$codes" \
        --expected-calls "${RANDOM_OUTPUT_LEN:-100}"
done <<'EOF'
topdown|0x0011,0x0008,0x003e,0x001b
icache|0x0008,0x0001,0x0014,0x0027,0x0028
dcache|0x0008,0x0003,0x0004,0x0017,0x0016
l3|0x0008,0x002a,0x002b
tlb1|0x0008,0x0002,0x0026,0x0005,0x0025
tlb2|0x0008,0x002d,0x002e,0x002f,0x0030
branch|0x0008,0x0021,0x0022
imix|0x001b,0x0070,0x0073,0x8005,0x0078
imix2|0x001b,0x0071,0x0079,0x007a,0x0075,0x8006
EOF

RUN_ROOT="$RUN_ROOT" "$COMMON_DIR/run_one.sh" hotspot
"$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT"
