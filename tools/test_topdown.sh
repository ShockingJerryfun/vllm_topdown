#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/infraperf-tests.XXXXXX")
trap 'rm -rf -- "$WORK"' EXIT
export PYTHONDONTWRITEBYTECODE=1

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -c \
    'import sys; from pathlib import Path; from bench.infer.llm.vllm.compat.compat import prepare_runtime; prepare_runtime(Path(sys.argv[1]))' \
    "$WORK/legacy"
mkdir -p "$WORK/legacy/tests"
cp -R "$ROOT/bench/infer/llm/vllm/tests" "$WORK/legacy/tests/tools"
PYTHONPATH="$ROOT:$WORK/legacy${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m pytest -q -p no:cacheprovider --basetemp "$WORK/pytest-legacy" \
    "$WORK/legacy/tests/tools"

mkdir -p "$WORK/probe"
"$PYTHON" "$ROOT/bench/infer/llm/vllm/probe/prepare.py" "$WORK/probe"
cp -R "$ROOT/collect/pmu/topdown/tests" "$WORK/probe/tests"
PYTHONPATH="$ROOT:$WORK/probe${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m pytest -q -p no:cacheprovider --basetemp "$WORK/pytest-probe" \
    "$WORK/probe/tests"

PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON" -m pytest -q -p no:cacheprovider --basetemp "$WORK/pytest-pipeline" \
    "$ROOT/common/runners/tests"
