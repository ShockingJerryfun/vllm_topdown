#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/infraperf-tests.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
export PYTHONDONTWRITEBYTECODE=1

PYTHONPATH="$ROOT" "$PYTHON" -c \
    'import sys; from pathlib import Path; from bench.infer.llm.vllm.compat.compat import prepare_runtime; prepare_runtime(Path(sys.argv[1]))' \
    "$WORK/legacy"
mkdir -p "$WORK/legacy/tests"
cp -R "$ROOT/bench/infer/llm/vllm/tests" "$WORK/legacy/tests/tools"
PYTHONPATH="$ROOT:$WORK/legacy" "$PYTHON" -m pytest -q -p no:cacheprovider \
    "$WORK/legacy/tests/tools"

mkdir -p "$WORK/probe"
"$PYTHON" "$ROOT/bench/infer/llm/vllm/probe/prepare.py" "$WORK/probe"
cp -R "$ROOT/collect/pmu/topdown/tests" "$WORK/probe/tests"
PYTHONPATH="$ROOT:$WORK/probe" "$PYTHON" -m pytest -q -p no:cacheprovider \
    "$WORK/probe/tests"

PYTHONPATH="$ROOT" "$PYTHON" -m pytest -q -p no:cacheprovider \
    "$ROOT/common/runners/tests"
