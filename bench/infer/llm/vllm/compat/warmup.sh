#!/usr/bin/env bash
# One request policy shared by the time and PMU rounds of one service session.
run_round_warmups() {
    local count=${ROUND_WARMUPS:-0}
    local marker="${SESSION_DIR:-$RUN_DIR}/warmup.completed"
    case ${WARMUP_SCOPE:-per_group} in
        per_group) ;;
        session)
            [[ -n ${SESSION_DIR:-} ]] || {
                printf 'Session warmup requires a persistent service\n' >&2
                return 2
            }
            [[ ! -e "$marker" ]] || count=0
            ;;
        *) printf 'Unknown WARMUP_SCOPE\n' >&2; return 2 ;;
    esac
    for (( warmup=0; warmup<count; warmup++ )); do
        run_benchmark "$RUN_DIR/warmup_$warmup.log"
        [[ "$BENCH_RC" -eq 0 ]] || return 5
    done
    if [[ ${WARMUP_SCOPE:-per_group} == session && "$count" -gt 0 ]]; then
        touch "$marker"
    fi
}
