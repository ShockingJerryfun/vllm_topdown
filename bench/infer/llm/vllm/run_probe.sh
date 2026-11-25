#!/usr/bin/env bash
set -Eeuo pipefail
DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$DIR/../../../.." && pwd)
mkdir -p "$ROOT/results/.runtime"
RUNTIME=$(mktemp -d "$ROOT/results/.runtime/probe.XXXXXX")
trap 'rm -rf -- "$RUNTIME"' EXIT
python3 "$DIR/probe/prepare.py" "$RUNTIME"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
bash "$RUNTIME/adapters/vllm/run.sh" "$@"
