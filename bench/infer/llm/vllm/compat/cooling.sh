#!/usr/bin/env bash
# Cool only between groups, before their warmup and formal requests.
cool_before_group() {
    local limit=${GROUP_START_TEMPERATURE:-0}
    [[ "$limit" =~ ^[0-9]+$ ]] || return 2
    (( limit > 0 )) || return 0
    (( limit < 85 )) || return 2
    local started=$SECONDS temperature
    printf 'elapsed_seconds\ttemperature_c\tlimit_c\n' > "$RUN_DIR/cooling.tsv"
    while true; do
        temperature=$(nvidia-smi -i "$GPU_ID" --query-gpu=temperature.gpu --format=csv,noheader,nounits) || return 6
        [[ "$temperature" =~ ^[0-9]+$ ]] || {
            printf 'Cannot read GPU temperature: %s\n' "$temperature" >&2
            return 6
        }
        printf '%s\t%s\t%s\n' "$((SECONDS - started))" "$temperature" "$limit" >> "$RUN_DIR/cooling.tsv"
        (( temperature <= limit )) && return 0
        (( SECONDS - started < 600 )) || {
            printf 'GPU did not cool to %s C within 600 seconds\n' "$limit" >&2
            return 6
        }
        sleep 5
    done
}
