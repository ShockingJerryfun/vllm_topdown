# Hygon C86-4G 7490 六阶段采集

本目录用于 vLLM 0.26.0 默认 V2、Qwen3-8B、Python 3.13 基线的六阶段
Core PMU 采集。

| 阶段 | 打点位置 |
| --- | --- |
| `update_states` | `vllm/v1/worker/gpu/model_runner.py` |
| `prepare_inputs` | `vllm/v1/worker/gpu/model_runner.py` |
| `forward` | `vllm/model_executor/models/qwen3.py` |
| `compute_logits` | `vllm/model_executor/models/qwen3.py` |
| `sample` | `vllm/v1/worker/gpu/model_runner.py` |
| `bookkeeping` | `vllm/v1/worker/gpu/model_runner.py` |

每个阶段在实际逻辑前调用 `kperf_begin()`，在 `finally` 中调用
`kperf_finish()`。`kperf_instrument.py` 通过 `perf_event_open` 创建 pinned
事件组，按当前线程计数，不调用 `perf stat`。每组不超过五个事件，结果包含
`time_enabled`、`time_running` 和有效性标记；不对未完整调度的计数做缩放。

| 组 | 事件 |
| --- | --- |
| base | cycles 0x76，instructions 0xc0，branches 0xc2，branch_misses 0xc3，retired_uops 0xc1 |
| uops_ls | cycles 0x76，instructions 0xc0，dispatched_uops 0x03aa，retired_uops 0xc1，ls_ops_dispatched 0x0729 |
| frontend | cycles 0x76，instructions 0xc0，ic_dq_empty 0x0287，ic_backpressure 0x0187，l1i_fetch_misses 0x81 |
| backend | cycles 0x76，instructions 0xc0，retire_token_stalls 0x40af，agsq_token_stalls 0x20af，alu_token_stalls 0x10af |
| dcache | instructions 0xc0，l1d_8byte_accesses 0x40，l2_data_requests 0xc860，l2_data_misses 0x0864，l2_data_hits 0x7064 |
| dtlb | instructions 0xc0，l1_dtlb_misses 0xff45，dtlb_l2_hits 0x0f45，dtlb_l2_misses 0xf045，data_page_walks 0x0346 |

汇总页中的前后端指标是独立诊断比例，不是 Intel Topdown，不能相加为
100%。L3/DF 属于共享 uncore PMU，在事件语义确认前保持“未采集”。

宿主机执行：

```bash
bash /home/f00955680/vllm_fj/scripts/run_topdown_hygon_v026.sh
```

默认参数为 Qwen3-8B、BF16、input 7000、output 100、并发 1、TP=1、
`max-model-len=16384`、`gpu-memory-utilization=0.8`。默认不启用
`--enforce-eager` 和 `--async-scheduling`，可通过 `SERVER_FLAGS` 增加。
