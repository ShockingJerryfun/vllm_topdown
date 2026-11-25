#!/usr/bin/env bash
set -Eeuo pipefail

RUN_ROOT=${1:?usage: analyze.sh RESULT_DIRECTORY}
source "$PROJECT/scripts/config.env"
SCRIPT_DIR="$PROJECT/scripts/$CHIP"
REPORT_CONFIG="$SCRIPT_DIR/report_config.json"

if [[ "$CHIP" == 920b && ${GROUP_SET_920B:-standard} == extended ]]; then
    REPORT_CONFIG="$SCRIPT_DIR/report_config_extended.json"
elif [[ "$CHIP" == zen4 ]]; then
    REPORT_CONFIG="$RUN_ROOT/report_config.json"
fi

"$PYTHON_BIN" "$SCRIPT_DIR/summary.py" "$RUN_ROOT"
BUILD_COMMAND=(
    "$PYTHON_BIN" "$PROJECT/scripts/build_xlsx.py" "$RUN_ROOT"
    --config "$REPORT_CONFIG"
    --chip "$CHIP"
    --version "$VLLM_VERSION_SHORT"
    --model-short "$MODEL_SHORT"
    --input-len "$RANDOM_INPUT_LEN"
    --output-len "$RANDOM_OUTPUT_LEN"
)
if [[ "$CHIP" != hygon_c86_7490 ]]; then
    BUILD_COMMAND+=(--compact-report --include-end-to-end)
fi
[[ ${SPE_ENABLE:-0} != 1 ]] || BUILD_COMMAND+=(--defer-spe)
"${BUILD_COMMAND[@]}"
