# analyze/latency/

时延原始数据 → 推理服务指标。

## 典型指标

- TTFT（Time To First Token）
- TPOT（Time Per Output Token）
- E2E latency、throughput（tokens/s、requests/s）
- P50/P90/P99 分位

输入 `results/collect/time/<run_id>/` 或 `results/bench/infer/**`，输出到 `results/analyze/latency/<run_id>/`。
