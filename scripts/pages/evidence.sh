#!/usr/bin/env bash
# Sourced at quiescent gates by the controlled experiment runner.
page_snapshot() {
    [[ -n ${CODE_PAGE_CONDITION:-} ]] || return 0
    [[ ${CODE_PAGE_MODE:-} == 4k || ${CODE_PAGE_MODE:-} == 64k ]] || {
        printf 'Set CODE_PAGE_MODE=4k or 64k for a private page condition\n' >&2
        return 2
    }
    local label=$1 worker identity page_dir="$RUN_DIR/pages" cohort="" directory=""
    if [[ ${RESUME_COLLECTION:-0} != 1 && -n ${SESSION_DIR:-} && ${KPERF_TARGET:-} == execute_model_to_sample_tokens \
        && ${KPERF_QUALIFIER:-} == run_fullgraph && "$LABEL" != spe ]]; then
        [[ $(basename "$RUN_ROOT") == end_to_end ]] || return 2
        cohort=persistent_end_to_end
        directory=end_to_end/pages
    elif [[ -n ${SESSION_DIR:-} && ${CHIP:-} == 920b && -z ${KPERF_TARGET:-} \
        && "$LABEL" != spe && "$LABEL" != hotspot ]]; then
        cohort=persistent_pipeline
        directory=pages
    fi
    if [[ -n "$cohort" ]]; then
        install -d -m 755 "$page_dir"
        printf '{"scope":"%s","directory":"%s"}\n' "$cohort" "$directory" \
            > "$page_dir/reference.json"
        page_dir="$RUN_ROOT/pages"
        if [[ "$label" == before && "$LABEL" != time ]] \
            || [[ "$label" == after && "$LABEL" != imix2 ]]; then
            return 0
        fi
    fi
    worker=$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1]))["worker"])' "$BIND_IDENTITY")
    identity="$CODE_PAGE_CONDITION/identity.json"
    if [[ "$LABEL" == spe ]]; then
        identity="$SPE_RUN/evidence/page_identity.json"
    fi
    placement_check check
    taskset -c "$SERVICE_CPUS" "$PYTHON_BIN" "$SCRIPT_DIR/pages/snapshot.py" \
        --pid "$worker" --node "$WORKER_NUMA_NODE" --identity "$identity" \
        --output "$page_dir/$label" --query "$SCRIPT_DIR/pages/process_pages"
    placement_check check
    if [[ "$label" == after ]]; then
        taskset -c "$SERVICE_CPUS" "$PYTHON_BIN" "$SCRIPT_DIR/pages/verify.py" \
            --before "$page_dir/before" --after "$page_dir/after" \
            --identity "$identity" --mode "$CODE_PAGE_MODE" --node "$WORKER_NUMA_NODE" \
            --coverage-policy "${CODE_PAGE_COVERAGE_POLICY:-strict}" \
            --residency-policy "${CODE_PAGE_RESIDENCY_POLICY:-strict}" \
            --output "$page_dir/verification.json" \
            || [[ ${CODE_PAGE_AUDIT_MODE:-strict} == observe ]]
    fi
}
