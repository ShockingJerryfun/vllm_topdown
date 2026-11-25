# analyze/

把 `collect/` 与 `bench/` 产出的原始数据算成**指标**。

## 子目录

- `pmu/` — PMU 事件 → IPC、CacheMissRate、stall 占比
- `gpu/` — GPU 原始数据 → MFU/HFU、kernel 耗时分布
- `latency/` — 时延原始数据 → TTFT、TPOT、throughput

## 约定

- 输入：`results/collect/**` 或 `results/bench/**` 的原始产物。
- 输出：统一 schema 的 JSON/CSV，列名与单位遵循 `common/schemas/`，落 `results/analyze/<kind>/<run_id>/`。
- 不重新采集数据；若缺数据应直接报错而非静默跳过。
- 计算公式与定义见 `docs/metrics.md`，代码实现与之对齐。
