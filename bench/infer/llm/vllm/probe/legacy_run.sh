#!/usr/bin/env bash
set -Eeuo pipefail

[[ $# == 2 ]] || { printf 'usage: run.sh CONFIG_ABSOLUTE_PATH NEW_RESULT_DIRECTORY\n' >&2; exit 2; }
ADAPTER_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
TOPDOWN_HOME=$(cd -- "$ADAPTER_DIR/../.." && pwd)
export TOPDOWN_HOME TOPDOWN_CONFIG=$1 RUN_ROOT=$2
export PYTHONPATH="$TOPDOWN_HOME${PYTHONPATH:+:$PYTHONPATH}"
export KPERF_IDENTITY_PREFIXES=vllm,torch
exec bash "$ADAPTER_DIR/scripts/920b/run.sh"
