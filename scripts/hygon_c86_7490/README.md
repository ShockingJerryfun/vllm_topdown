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
time 使用不打开 PMU 的独立轮次采集，避免 PMU 控制开销进入墙钟时间。

| 组 | 事件 |
| --- | --- |
| topdown | cycles 0x76，instructions 0xc0，retired_uops 0xc1，dispatched_uops 0x03aa，frontend_stall_any 0x0487 |
| branch | instructions 0xc0，branches 0xc2，branch_misses 0xc3 |
| spec_ls | dispatched_uops 0x03aa，load_ops 0x0129，store_ops 0x0229，load_store_ops 0x0429，branches 0xc2 |
| spec_ase | dispatched_uops 0x03aa，fpu_spec_uops 0x0f00 |
| icache | instructions 0xc0，l1i_fetch_windows 0x80，l1i_miss_windows 0x81，l2i_accesses 0x0764，l2i_misses 0x0164 |
| dcache | instructions 0xc0，ls_ops 0x0729，l1d_miss_proxy 0xc860，l2d_misses 0x0864，l2d_hits 0xf064 |
| tlb | instructions 0xc0，ls_ops 0x0729，l1_dtlb_misses 0xff45，stlb_hits 0x0f45，stlb_misses 0xf045 |

L3 使用独立 Uncore 轮次：access 为
`amd_l3/event=0x04,umask=0xff/`，miss 为
`amd_l3/event=0x06,umask=0x01/`。采集器读取 `amd_l3/cpumask`，以
`pid=-1` 在每个代表 CPU 上开启事件并对所有 L3 域求和。该结果是
共享域归因，不是严格线程归因。

汇总页在 `cycles` 行下方显示 `cycle占比`，计算方式为
该阶段平均 cycles 除以八个阶段平均 cycles 之和。`频率(MHz)` 使用
该阶段平均 cycles 除以独立 time 轮次的平均 `time(us)` 估算；两个值来自
不同采集轮次，仅供观察，不用于计算 `CPU利用率`。明细页在事件名称下方
显示对应原始事件号。

所有阶段比率按 `SUM(分子)/SUM(分母)` 计算。Retire、FrontendBound、
BackendBound、BadSpec 和 spec 分类都是 `Hygon Zen1 proxy`，不能与
920B 当作完全同源事件比较；L1D 是由 L2 请求反推的 miss proxy，
Topdown 或 spec 分类的原始汇总超过 105% 时显示“无效”，不强制归一化。
STLB 仅表示 D-side STLB。C86-7490 上的 0x84、0x85、0x94、0x99 在
受控测试中不响应，ITLB 两行明确显示“未支持”。`CPU利用率` 使用独立
time 轮次中当前线程 CPU 时间除以墙钟时间，脚本不裁剪实测结果。

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
