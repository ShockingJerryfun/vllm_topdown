#!/usr/bin/env bash
# Sourced by run_one after the benchmark command is assembled.
: "${SPE_RUN:?set SPE_RUN}"
: "${SPE_EXPECTED_CALLS:?set SPE_EXPECTED_CALLS}"
[[ "$NUM_PROMPTS" == 1 && "$NUM_WARMUPS" == 0 && "$READY_CHECK_TIMEOUT_SEC" == 0 ]] || {
    printf 'SPE controls its own one warmup and three formal requests\n' >&2; exit 2;
}
spe_wait() {
    local name=$1
    local limit=$(( SECONDS + 900 ))
    while [[ ! -e "$SPE_RUN/gates/$name" ]]; do
        [[ ! -e "$SPE_RUN/gates/abort" ]] || return 1
        kill -0 "$SERVICE_PID" 2>/dev/null || return 1
        (( SECONDS < limit )) || return 1
        sleep 0.2
    done
}
cp "$BIND_IDENTITY" "$SPE_RUN/gates/ready.tmp"
mv "$SPE_RUN/gates/ready.tmp" "$SPE_RUN/gates/ready.json"
for request in 0 1 2 3; do
    spe_wait "go_$request"
    placement_check check
    run_benchmark "$RUN_DIR/request_$request.log"
    [[ "$BENCH_RC" -eq 0 ]] || exit 5
    "$PYTHON_BIN" - "$SPE_RUN" "$BIND_IDENTITY" "$request" "$SPE_EXPECTED_CALLS" <<'PY'
import json
import sys
from pathlib import Path

root, identity, request, expected = sys.argv[1:]
worker = json.loads(Path(identity).read_text())["worker"]
rows = json.loads((Path(root) / "data" / f"windows_{worker}_{request}.json").read_text())
if len(rows) != int(expected) or any(row["tid"] != worker for row in rows):
    raise RuntimeError("Incomplete SPE request windows or incorrect execution TID")
PY
    [[ "$request" != 0 ]] || page_snapshot before
    touch "$SPE_RUN/gates/done_$request"
done
spe_wait recording_stopped
page_snapshot after
touch "$SPE_RUN/gates/pages_done"
spe_wait complete
