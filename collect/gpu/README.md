# collect/gpu/

GPU 指标采集脚本。

## 工具

- `nsys` — Nsight Systems 全系统 trace
- `ncu` — Nsight Compute 单 kernel profiling
- `dcgm` — DCGM 时序采样（利用率、显存、功耗）
- `smi` — nvidia-smi 轮询

## 输出

原始 nsys/ncu 报告、DCGM csv 落 `results/collect/gpu/<run_id>/`。
MFU/HFU、kernel 耗时分布等在 `analyze/gpu/`。
