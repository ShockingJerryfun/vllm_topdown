# Hygon C86-4G 7490 八阶段采集

本目录用于 vLLM 0.26.0 默认 V2、Qwen3-8B、Python 3.13 基线的八阶段
Core PMU 采集。

| 阶段 | 打点位置 |
| --- | --- |
| `add_requests` | `vllm/v1/worker/gpu/model_runner.py` |
| `prepare_inputs` | `vllm/v1/worker/gpu/model_runner.py` |
| `prepare_attn_runner` | `vllm/v1/worker/gpu/model_runner.py` |
| `prepare_attn_model_state` | `vllm/v1/worker/gpu/model_states/default.py` |
| `run_fullgraph` | `vllm/v1/worker/gpu/cudagraph_utils.py` |
| `sample` | `vllm/v1/worker/gpu/model_runner.py` |
| `async_output_init` | `vllm/v1/worker/gpu/async_utils.py` |
| `postprocess_sampled` | `vllm/v1/worker/gpu/model_runner.py` |

每个阶段在实际逻辑前调用 `kperf_begin()`，在 `finally` 中调用
`kperf_finish()`。根目录的 `kperf_instrument.py` 通过 `perf_event_open` 创建 pinned
事件组，按当前线程计数，不调用 `perf stat`。每组不超过五个事件，结果包含
`time_enabled`、`time_running` 和有效性标记；不对未完整调度的计数做缩放。
time使用不打开PMU的独立轮次采集，避免PMU控制开销进入墙钟时间。

| 组 | 事件 |
| --- | --- |
| base | cycles 0x76，instructions 0xc0，branches 0xc2，branch_misses 0xc3，retired_uops 0xc1 |
| uops_ls | cycles 0x76，instructions 0xc0，dispatched_uops 0x03aa，retired_uops 0xc1，ls_ops_dispatched 0x0729 |
| frontend | cycles 0x76，instructions 0xc0，ic_dq_empty 0x0287，ic_backpressure 0x0187，l1i_fetch_misses 0x81 |
| backend | cycles 0x76，instructions 0xc0，retire_token_stalls 0x40af，agsq_token_stalls 0x20af，alu_token_stalls 0x10af |
| dcache | instructions 0xc0，l1d_accesses 0x40，l2_request_activity 0xe860，l2_demand_misses 0x0864，l2_demand_hits 0xf064 |
| dtlb | instructions 0xc0，l1_dtlb_misses 0xff45，dtlb_l2_hits 0x0f45，dtlb_l2_misses 0xf045，data_page_walks 0x0346 |

`0x60` 统计 L2 request activity，`0x64` 统计 L2 demand hit/miss，两者范围
不同。报表分别输出 `L2 request activity MPKI` 和
`L2 demand access MPKI`，不再计算二者之间的 coverage；L2 hit ratio 和
miss ratio 均以 demand hit + miss 为分母。L1I 指标显示为
`L1I 32B fetch-window miss MPKI`，公式保持不变。

汇总页在 `cycles` 行下方显示 `cycles占八阶段总cycles比例`，计算方式为
该阶段平均 cycles 除以八个阶段平均 cycles 之和。

汇总页的指标行、名称和顺序与920B模板完全一致。Hygon没有等价事件语义的
Topdown、L1I、L1D、L3等指标直接显示“未采集”；Hygon原生的流水线、L1I和
L2 request/demand诊断指标保留在各事件组明细sheet中。`CPU利用率` 使用独立
time轮次中当前线程CPU时间除以墙钟时间，脚本不裁剪实测结果。

容器内先安装一次报表依赖：

```bash
/opt/vllm/bin/python3 -m pip install \
  -r /home/fj/vllm_fj/scripts/requirements-report.txt
```

宿主机执行：

```bash
CHIP=hygon_c86_7490 bash /home/fj/vllm_fj/scripts/run_topdown.sh
```

默认参数为 Qwen3-8B、BF16、input 7000、output 100、并发 1、TP=1、
`max-model-len=8192`、`gpu-memory-utilization=0.8`。默认启用
`--async-scheduling`，可通过 `SERVER_FLAGS` 覆盖。输出100对应99个完整
decode轮次；原始记录全部保留，统计时只选择连续命中八阶段的decode轮次。
默认最终工作簿为 `hygon_c86_7490_vllm0.26_qwen3_7k100.xlsx`。
