# collect/

采集原始数据，**只采不算**。

## 子目录

- `pmu/` — CPU PMU 事件（perf / perfetto / PEBS）
- `gpu/` — GPU 指标（nsys / ncu / DCGM / nvidia-smi 采样）
- `time/` — wall-clock、阶段耗时打点
- `power/` — 功耗 / 温度

## 约定

- 采集脚本输出原始事件流/日志到 `results/collect/<target>/<run_id>/`，不做任何聚合计算。
- 采集频率、事件列表等参数从 `configs/` 读取。
- 与被测进程的 attach/detach、后台采样启停统一走 `common/runners/`。
- 解析原始格式（perf.data、ncu-rep 等）放 `common/parsers/`，本目录只产原始文件。
