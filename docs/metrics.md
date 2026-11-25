# 指标定义 (metrics)

本文件定义所有 `analyze/` 产出的指标及其计算公式。代码实现须与此对齐。
新增指标先在此登记，再实现。

vLLM Topdown 的事件组位于配置或各采集器的 `profiles/` 中，公式按 CPU 平台维护于 `analyze/pmu/topdown/`。下面的通用 stall 比值不是各芯片 Topdown Bound 公式；结果中未支持的指标保留为空。

## 推理时延 (analyze/latency/)

| 指标 | 定义 | 单位 |
|---|---|---|
| TTFT | 从请求发出到收到首个 token 的时间 | ms |
| TPOT | 收到首 token 后，每个后续 output token 的平均间隔 | ms |
| e2e_latency | 从请求发出到接收完最后一个 token | ms |
| throughput_tokens | output_tokens / 总时长 | tokens/s |
| throughput_requests | 完成请求数 / 总时长 | req/s |

分位：P50 / P90 / P99 在 schema 中以 `ttft_ms_p50` 等列名给出。

## PMU (analyze/pmu/)

| 指标 | 定义 |
|---|---|
| IPC | instructions / cycles |
| cache_miss_rate | cache-misses / cache-references |
| branch_miss_rate | branch-misses / branch-instructions |
| frontend_stall_pct | frontend-stall-cycles / cycles |
| backend_stall_pct | backend-stall-cycles / cycles |

## GPU (analyze/gpu/)

| 指标 | 定义 |
|---|---|
| MFU | 实际 FLOPs / (SM数 × FPU吞吐 × 时钟 × 时长) |
| HFU | 硬件 FLOP 利用率（含非 matmul） |
| gpu_mem_bw_util | 实际显存带宽 / 峰值带宽 |
| kernel_time_share | 单 kernel 耗时 / 总 kernel 耗时 |

## 功耗 (collect/power/ → analyze 按需)

| 指标 | 定义 | 单位 |
|---|---|---|
| avg_power | 采样窗口内平均功率 | W |
| energy | ∫power dt | J |
| energy_per_token | energy / output_tokens | J/token |
