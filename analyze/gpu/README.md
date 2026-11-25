# analyze/gpu/

GPU 原始数据 → 指标。

## 典型指标

- MFU = 实际 FLOPs / (SM数 × FPU吞吐 × 时钟 × 时长)
- HFU（硬件 FLOP 利用率）
- kernel 耗时分布 / top-k kernel
- 显存带宽利用率

输入 `results/collect/gpu/<run_id>/`，输出到 `results/analyze/gpu/<run_id>/`。
公式定义见 `docs/metrics.md`。
