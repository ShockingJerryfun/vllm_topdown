"""Emit deterministic vLLM-shaped logs for an offline scheduler test."""

import json
import os
import sys
from pathlib import Path

STAGES = (
    "add_requests", "prepare_inputs", "prepare_attn_runner", "prepare_attn_model_state",
    "run_fullgraph", "sample", "async_output_init", "postprocess_sampled",
)
COUNTS = {
    "0x76": 1000, "0x1000001a0": 900, "0x100001ea0": 1200,
    "0x1000060a0": 60, "0x7aa": 3000, "0xc1": 2400,
    "0x1000000c2": 300, "0xc0": 2500, "0x1060001a0": 50,
    "0xa2d6": 40, "0x2d6": 100, "0xc3": 30, "0x96": 10,
    "0xf960": 1000, "0xff70": 100, "0xff71": 50, "0xff72": 20,
    "0x964": 130, "0xc2": 600,
}


def main() -> None:
    label = sys.argv[1]
    codes = sys.argv[2].split(",") if len(sys.argv) > 2 else []
    target = os.getenv("KPERF_TARGET", "")
    qualifier = os.getenv("KPERF_QUALIFIER", "")
    with Path(os.environ["ROUND_RECORD"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"label": label, "codes": codes, "target": target,
                                 "qualifier": qualifier}) + "\n")
    directory = Path(os.environ["RUN_ROOT"]) / label
    directory.mkdir(parents=True)
    if label in {"benchmark_before", "benchmark_after"}:
        (directory / "benchmark.log").write_text(
            "Serving Benchmark Result\nSuccessful requests: 1\n"
            "Mean TTFT (ms): 100.0\nMean TPOT (ms): 4.0\n" + "=" * 50 + "\n"
        )
        return
    if label == "hotspot":
        (directory / "perf_report.txt").write_text(
            " 12.34%  VLLM::Worker_TP0  libpython.so  [.] fixture_function\n"
        )
        return

    mode = "time" if label == "time" else "pmu"
    (directory / "server.log").write_text(f"[kperf] enabled: mode={mode} names={codes}\n")
    lines = []

    def record(stage: str, call: int) -> None:
        if mode == "time":
            lines.append(f"KPERF_TIME,{stage},{call},100000,80000,1")
        else:
            running = 900 if os.getenv("FAKE_MULTIPLEX") == "1" else 1000
            counts = ",".join(str(COUNTS[code]) for code in codes)
            lines.append(f"KPERF,{stage},{call},1000,{running},1,{counts}")

    if target:
        record(target, 1)
        for call in (2, 3):
            record(target, call)
            lines.append(f"KPERF_QUALIFIER,{target},{call},{qualifier}")
    else:
        record("add_requests", 1)
        call = 2
        for _ in range(2):
            for stage in STAGES:
                record(stage, call)
                call += 1
    (directory / "measurement.log").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
