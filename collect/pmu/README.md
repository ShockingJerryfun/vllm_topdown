# collect/pmu/

CPU PMU 事件采集脚本。

## 工具

- `perf` — perf stat/record
- `perfetto` — 系统级 trace
- PEBS / AMD IBS 等

## 输出

原始 `perf.data` / perfetto trace / `perf stat` 文本落 `results/collect/pmu/<run_id>/`。
事件→指标（IPC、CacheMissRate、stall 占比）的计算在 `analyze/pmu/`。
