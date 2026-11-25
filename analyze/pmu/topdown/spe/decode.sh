#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/infraperf-spe.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
export PYTHONPATH="$ROOT:$WORK/source${PYTHONPATH:+:$PYTHONPATH}"
"$PYTHON" -c \
    'import sys; from pathlib import Path; from bench.infer.llm.vllm.compat.compat import prepare_runtime; prepare_runtime(Path(sys.argv[1]))' \
    "$WORK/source"
"$PYTHON" "$WORK/source/scripts/spe/decode.py" "$@"
