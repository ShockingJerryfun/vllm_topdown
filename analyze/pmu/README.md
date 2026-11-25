# analyze/pmu/

PMU 事件 → 指标。

## 典型指标

- IPC = instructions / cycles
- CacheMissRate = cache-misses / cache-references
- 分支预测失误率
- stall 周期占比（frontend / backend）

输入 `results/collect/pmu/<run_id>/`，输出 schema 化 JSON/CSV 到 `results/analyze/pmu/<run_id>/`。
公式定义见 `docs/metrics.md`。
