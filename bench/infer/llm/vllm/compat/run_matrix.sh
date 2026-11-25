#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
set -Eeuo pipefail

# Trusted shell configuration; preview is the default and has no side effects.
[[ $# -ge 1 && $# -le 2 ]] || { echo 'usage: run_matrix.sh MATRIX.env [--plan|--run]'; exit 2; }
matrix_config=$(realpath "$1")
action=${2:---plan}
[[ $action == --plan || $action == --run || $action == --resume ]] || exit 2
# shellcheck source=/dev/null
source "$matrix_config"
: "${BASE_CONFIG:?}" "${BATCH_ROOT:?}" "${CHIP:?}" "${PAGE_IDENTITY:?}"
: "${NEAR_CPUS:?}" "${NEAR_POOL:?}" "${NEAR_NODE:?}"
: "${FAR_CPUS:?}" "${FAR_POOL:?}" "${FAR_NODE:?}"
: "${SERVICE_CPUS:?}" "${CLIENT_CPUS:?}" "${CODE_GUARD:?}"
repeats=${REPEATS:-1}
[[ $repeats =~ ^[1-9][0-9]*$ && $BATCH_ROOT == /* && $BASE_CONFIG == /* ]] || exit 2
[[ $CHIP == 920b || $CHIP == 950 ]] || exit 2
[[ ${#MODEL_NAMES[@]} -gt 0 && ${#MODEL_NAMES[@]} -eq ${#MODEL_PATHS[@]} ]] || exit 2
order=(64k_near 4k_near 4k_far 64k_far)
for ((r=1; r<=repeats; r++)); do
    for ((m=0; m<${#MODEL_NAMES[@]}; m++)); do
        [[ ${MODEL_NAMES[m]} =~ ^[a-zA-Z0-9_-]+$ ]] || exit 2
        for ((k=0; k<4; k++)); do
            item=${order[$(((k+r-1)%4))]}
            printf '%s/%s/%s/r%s\n' "$BATCH_ROOT" "${MODEL_NAMES[m]}" "$item" "$r"
        done
    done
done
[[ $action != --plan ]] || exit 0

for tool in jq flock docker nvidia-smi sha256sum numactl; do command -v "$tool" >/dev/null; done
if [[ $action == --resume ]]; then
    cmp "$matrix_config" "$BATCH_ROOT/control/matrix.env"
    cmp "$BASE_CONFIG" "$BATCH_ROOT/control/base.env"
else
    [[ ! -e $BATCH_ROOT ]] || { echo 'Batch exists; use --resume to continue.' >&2; exit 2; }
fi
# Read collector defaults in an isolated shell: matrix topology remains authoritative.
base_value() {
    bash -c 'set -a; source "$1"; printenv "$2"' bash "$BASE_CONFIG" "$1"
}
project=$(base_value PROJECT)
container=$(base_value CONTAINER)
host_python=$(base_value HOST_PYTHON)
container_python=$(base_value PYTHON_BIN)
collection_lock=$(base_value EXPERIMENT_LOCK)
tools="$project/scripts/pages"
[[ -f $PAGE_IDENTITY && -x $tools/prefault && -x $host_python && -f $CODE_GUARD ]]
[[ -f $project/scripts/run_topdown.sh ]]
exec 8> "${collection_lock}.matrix"
flock -n 8 || { echo 'Another matrix is running' >&2; exit 2; }

idle_gpu() {
    local pids
    pids=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits)
    [[ -z $pids ]] || { echo 'GPU occupied; leaving workload untouched' >&2; return 1; }
}
wait_idle_gpu() {
    while ! idle_gpu; do
        nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader >&2
        sleep 15
    done
}
wait_idle_gpu
[[ $(docker inspect -f '{{json .Config.Cmd}}' "$container") == '["sleep","infinity"]' ]]
original_running=$(docker inspect -f '{{.State.Running}}' "$container")
mkdir -p "$BATCH_ROOT/control" "$BATCH_ROOT/configs" "$BATCH_ROOT/conditions"
if [[ $action == --run ]]; then
    cp "$matrix_config" "$BATCH_ROOT/control/matrix.env"
    cp "$BASE_CONFIG" "$BATCH_ROOT/control/base.env"
fi
sha256sum "$0" "$project/scripts/run_topdown.sh" >> "$BATCH_ROOT/control/entry.sha256"
setting=${THP_EXEC_SETTING:-/sys/kernel/mm/transparent_hugepage/thp_exec_enabled}
original_thp=$(cat "$setting")
[[ $original_thp == 0x0 ]] || { echo 'Expected thp_exec_enabled=0x0 before preparation' >&2; exit 2; }
setting_changed=0
finish() {
    local rc=$?
    trap - EXIT
    if ((setting_changed)); then printf '%s\n' "$original_thp" > "$setting"; fi
    # Never stop a container with surviving task processes or another workload.
    if [[ $original_running == false && $(docker inspect -f '{{.State.Running}}' "$container") == true ]]; then
        if [[ $(docker top "$container" -eo pid | wc -l) -eq 2 ]]; then docker stop "$container" >/dev/null; fi
    fi
    printf '%s\n' "$rc" > "$BATCH_ROOT/control/exit_code"
    exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
[[ $original_running == true ]] || docker start "$container" >/dev/null
[[ $(docker top "$container" -eo pid | wc -l) -eq 2 ]]
jq -r '.files[] | select(.data_only != true) | .path' "$PAGE_IDENTITY" > "$BATCH_ROOT/control/code_paths.txt"
jq -r '.files[] | select(.data_only == true) | .path' "$PAGE_IDENTITY" > "$BATCH_ROOT/control/data_paths.txt"
[[ -s $BATCH_ROOT/control/code_paths.txt ]]

for path in "${MODEL_PATHS[@]}"; do
    [[ -s $path/config.json && -s $path/model.safetensors.index.json ]]
    jq -e '.weight_map | type == "object" and length > 0' "$path/model.safetensors.index.json" >/dev/null
    while IFS= read -r shard; do [[ -s $path/$shard ]]; done < <(jq -r '.weight_map[]' "$path/model.safetensors.index.json" | sort -u)
done
[[ -f $BATCH_ROOT/index.csv ]] || printf 'model,condition,repeat,seconds,report\n' > "$BATCH_ROOT/index.csv"
for ((r=1; r<=repeats; r++)); do
    for ((m=0; m<${#MODEL_NAMES[@]}; m++)); do
        model=${MODEL_NAMES[m]}
        selected_code_paths="$BATCH_ROOT/control/${model}_code_paths.txt"
        if [[ -f $selected_code_paths ]]; then
            sort -u "$BATCH_ROOT/control/code_paths.txt" "$selected_code_paths" > "$BATCH_ROOT/control/${model}_merged_paths.txt"
            selected_code_paths="$BATCH_ROOT/control/${model}_merged_paths.txt"
        else
            selected_code_paths="$BATCH_ROOT/control/code_paths.txt"
        fi
        for ((k=0; k<4; k++)); do
            item=${order[$(((k+r-1)%4))]}
            mode=${item%_*}; locality=${item#*_}
            if [[ $locality == near ]]; then
                cpus=$NEAR_CPUS; pool=$NEAR_POOL; node=$NEAR_NODE
            else
                cpus=$FAR_CPUS; pool=$FAR_POOL; node=$FAR_NODE
            fi
            condition="$BATCH_ROOT/conditions/${model}_${item}"
            run="$BATCH_ROOT/$model/$item/r$r"
            retry_file="$BATCH_ROOT/control/retries/${model}_${item}_r$r"
            if [[ -f $retry_file ]]; then
                retry_name=$(cat "$retry_file")
                [[ $retry_name =~ ^r[0-9]+_retry[0-9]+$ ]] || exit 2
                run="$BATCH_ROOT/$model/$item/$retry_name"
            fi
            if [[ -s $run/acceptance.json ]] && jq -e '.status == "pass"' "$run/acceptance.json" >/dev/null; then
                jq -e '.status == "pass"' "$run/evidence/cleanup.json" >/dev/null
                [[ -s $(jq -r .report "$run/complete.json") ]]
                printf 'Already accepted: %s\n' "$run"
                continue
            fi
            [[ ! -e $run ]] || { echo "Unaccepted run needs diagnosis: $run" >&2; exit 2; }
            config="$BATCH_ROOT/configs/${model}_${item}_r$r.env"
            wait_idle_gpu
            available=$(df -Pk "$BATCH_ROOT" | awk 'NR==2 {print $4}')
            ((available >= ${MIN_FREE_KIB:-20971520})) || { echo 'Less than required free disk space' >&2; exit 2; }
            # Preparation holds the same lock used by the collector.
            exec 9> "$collection_lock"
            flock -n 9
            wait_idle_gpu
            if [[ ! -d $condition ]]; then
                [[ $(cat "$setting") == "$original_thp" ]]
                docker exec "$container" numactl --physcpubind="${cpus%%,*}" --membind="$node" \
                    "$container_python" "$tools/prepare.py" --source-root / \
                    --paths "$selected_code_paths" --data-paths "$BATCH_ROOT/control/data_paths.txt" \
                    --code-guard "$CODE_GUARD" --output "$condition" \
                    > "$BATCH_ROOT/control/${model}_${item}_prepare.log" 2>&1
            fi
            [[ -f $condition/identity.json ]]
            # Pending conditions may have been cached since their earlier preparation.
                "$host_python" "$tools/cold.py" "$condition"
                prefault_work=$(mktemp -d "$condition/prefault.XXXXXX")
                if [[ $mode == 64k ]]; then setting_changed=1; printf '0x2\n' > "$setting"; fi
                "$tools/prefault" "$mode" "${cpus%%,*}" "$node" \
                    "$condition/segments.tsv" "$condition/files" "$prefault_work/prefault.tsv" 1
                [[ ! -f $condition/prefault.tsv ]] || cp "$condition/prefault.tsv" "$prefault_work/previous.tsv"
                cp "$prefault_work/prefault.tsv" "$condition/prefault.tsv"
                printf '%s\n' "$original_thp" > "$setting"
                [[ $(cat "$setting") == "$original_thp" ]]
                setting_changed=0
            flock -u 9
            exec 9>&-
            cp "$BATCH_ROOT/control/base.env" "$config"
            {
                printf '\nCHIP=%q\nMODEL=%q\nMODEL_SHORT=%q\nSERVED_MODEL=%q\n' "$CHIP" "${MODEL_PATHS[m]}" "$model" "$model"
                printf 'WORKER_CPUS=%q\nWORKER_POOL_CPUS=%q\nWORKER_NUMA_NODE=%q\n' "$cpus" "$pool" "$node"
                printf 'SERVICE_CPUS=%q\nCLIENT_CPUS=%q\n' "$SERVICE_CPUS" "$CLIENT_CPUS"
                printf 'CODE_PAGE_CONDITION=%q\nCODE_PAGE_MODE=%q\n' "$condition" "$mode"
                printf '%s\n' 'SERVER_FLAGS=--no-async-scheduling' 'COLLECTION_PROFILE=full' 'SPE_ENABLE=1' \
                    'FREQUENCY_ENABLE=1' 'PLACEMENT_MODE=worker_set' 'ROUND_WARMUPS=1' 'WARMUP_SCOPE=per_group' \
                    'GROUP_START_TEMPERATURE=0' 'GPU_THERMAL_LIMIT=0' 'CODE_PAGE_COVERAGE_POLICY=strict' \
                    'CODE_PAGE_RESIDENCY_POLICY=observe' 'CODE_PAGE_AUDIT_MODE=strict'
            } >> "$config"
            started=$SECONDS
            # Existing supervisor owns thermal aborts, child cleanup and acceptance.
            log="$BATCH_ROOT/control/${model}_${item}_r$r.log"
            while true; do
                wait_idle_gpu
                if bash "$project/scripts/run_topdown.sh" "$config" "$run" >> "$log" 2>&1; then break; fi
                # A workload can appear after the previous check. Retry only this
                # pre-start race; never restart an existing failed measurement.
                [[ ! -e $run ]] && tail -10 "$log" | grep -q 'GPU already has a compute workload' || exit 1
                nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader >> "$log"
            done
            jq -e '.status == "pass"' "$run/acceptance.json" >/dev/null
            jq -e '.status == "pass"' "$run/evidence/cleanup.json" >/dev/null
            report=$(jq -r '.report' "$run/complete.json")
            [[ -s $report ]]
            printf '%s,%s,%s,%s,%s\n' "$model" "$item" "$r" "$((SECONDS-started))" "$report" >> "$BATCH_ROOT/index.csv"
        done
    done
done
printf 'All runs accepted: %s/index.csv\n' "$BATCH_ROOT"
