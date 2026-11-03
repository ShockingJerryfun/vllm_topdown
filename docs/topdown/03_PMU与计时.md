# PMU 指标与计时说明

本手册说明阶段边界、有效样本、事件组、计算公式和结果解释。运行命令见 [使用指南](01_使用指南.md)，条件设置见 [测试方法](02_测试方法.md)。公式中的事件编码必须与 `CHIP` 对应，不能跨平台套用。

## 1. 测量对象与范围

### 1.1 父阶段和八阶段

| 标识 | 测量对象 |
| --- | --- |
| `execute_model_to_sample_tokens` | 从模型执行进入到采样相关流程完成的父阶段 CPU 路径 |
| `add_requests` | 向 Runner 添加本轮请求 |
| `prepare_inputs` | 准备本轮输入 |
| `prepare_attn_runner` | Runner 侧 Attention 准备 |
| `prepare_attn_model_state` | 模型状态侧 Attention 准备 |
| `run_fullgraph` | 完整 CUDA Graph 执行的 Host 调用路径 |
| `sample` | 采样调用路径 |
| `async_output_init` | 异步输出对象初始化 |
| `postprocess_sampled` | 采样结果后处理 |

父阶段用 `run_fullgraph` 命中条件筛选目标 Decode。修改 Eager、Graph 或 Runner 模式后，必须重新检查此条件是否还能命中，不能把零样本解释为零耗时。

父阶段与八阶段分开运行；不同 PMU 组也使用独立请求。八阶段可在同一组的请求中依次打点，但不能用它们的和减去另一轮父阶段结果来计算“未覆盖耗时”，也不能把跨轮结果拼成同一次请求的绝对时间线。报表中的子阶段 `cycle占比` 是各子阶段平均 cycles 的归一化比较，不是相对于父阶段实测 cycles 的覆盖率。

### 1.2 PMU 统计范围

普通 Core PMU 在当前执行线程上调用 `perf_event_open(pid=0, cpu=-1)`，不继承到其他线程；事件组固定调度，排除 Hypervisor，但没有排除内核执行。因此它不同于热点使用的 `cycles:u`，也不代表整个 Worker 的所有线程、所有 CPU 或 GPU。

Hygon L3 使用 `amd_l3` uncore PMU，按 PMU 的 CPU/domain 范围读取后汇总。它没有 Core PMU 的线程隔离语义，必须说明共享域内的其他活动。

## 2. 样本筛选和聚合

按以下顺序处理数据：

1. 确认请求成功、输入输出 token 数和执行模式符合配置。
2. 按阶段及 `run_fullgraph` 标记选择目标 Decode，排除 Prefill 和预热。
3. 检查线程身份、调用编号和事件记录完整性。
4. PMU 要求 `time_enabled > 0` 且 `time_enabled == time_running`。不满足时标为无效，不用复用缩放把它转为正式阶段结果。
5. 在有效行上求事件总和，再计算比值；同时保留原始行数、选中数、有效数和无效数。

单请求、固定输出长度为 `N` 时，当前解析器以 `N−1` 个目标 Decode 为预期。改变请求数、并发或 EOS 行为后，应修改并核对预期，不能直接沿用。

本文记号：

| 记号 | 含义 |
| --- | --- |
| `E_g(x)` | 事件组 `g` 中，同一阶段有效行的事件 `x` 之和 |
| `C_g` | 该组的周期数；Arm 为 `E_g(0x0011)` |
| `I_g` | 该组的退休指令数；Arm 为 `E_g(0x0008)` |
| `S_g` | Arm 的 speculative instruction 计数 `E_g(0x001b)` |
| `mean(x)` | 有效调用上字段 `x` 的算术平均 |

后面的表格在“取数组”内省略下标，例如 `E(0x7005)/C`。百分比在展示时乘 100%，原始计算保留小数。缺事件、缺组或分母为零时记录未采集／不支持／无效，不填零。

**示例：**两次调用分别有 10/100 和 30/1000 次 miss/access，汇总 missrate 为 `40/1100=3.64%`，不是两个 missrate 的均值 `6.5%`。MPKI 为 `1000×miss/instructions`，与 missrate 的分母不同。

## 3. 920B 公式

### 3.1 Topdown 一级和前端

920B 使用宽度 6。`Retire`、`FrontendBound`、`BadSpec` 是槽位模型，不能直接解释成各部分实测耗时。

| 指标 | 公式 | 取数组 |
| --- | --- | --- |
| IPC | `I/C` | `topdown` |
| Retire | `I/(6C)` | `topdown` |
| FrontendBound | `E(0x2011)/(6C)` | `topdown` |
| Fetch Latency Bound | `E(0x2012)/C` | `topdown` |
| Fetch Bandwidth Bound | `FrontendBound − Fetch Latency Bound` | `topdown` |
| BadSpec | `(S−I)/(6C)` | `topdown` |
| BackendBound | `1−Retire−FrontendBound−BadSpec` | `topdown` |
| Idle by iCache Miss | `[15×(E(0x0027)−E(0x0028))+100×E(0x0028)]/C` | `icache` |
| Idle by iTLB Miss | `[15×(E(0x0030)−E(0x002e))+100×E(0x002e)]/C` | `tlb2` |

两个 Idle 指标使用 15/100 的延迟模型系数，属于估算代理指标；不是实际逐次 miss 延迟，也不能与 SPE 平均延迟混用。残差为负或明显超出合理范围时，检查事件语义、有效性和组间稳定性，不通过裁剪制造正常结果。

### 3.2 分支与 Flush

| 指标 | 公式 | 取数组 |
| --- | --- | --- |
| Branch Flush | `5×E(0x0010)/C` | `flush` |
| OoO Flush | `9×E(0x2010)/C` | `flush` |
| SP Flush | `5×E(0x104f)/C` | `flush` |
| Flush | 上述三项之和 | `flush` |
| Branch Mispredicts | `BadSpec×E_flush(0x0010)/(E_flush(0x0010)+E_flush(0x2010))` | 跨 `topdown`、`flush` |
| Machine Clears | `BadSpec−Branch Mispredicts` | 跨组派生 |
| Indirect Branch | `E(0x1010)/E(0x0010)` | `badspec_branch` |
| Push Branch | `(E(0x1013)+E(0x1016))/E(0x0010)` | `badspec_branch` |
| Pop Branch | `E(0x100d)/E(0x0010)` | `badspec_branch` |
| Other Branch | `1−(E(0x1013)+E(0x1016)+E(0x100d))/E(0x0010)` | `badspec_branch` |
| Nuke Flush | `E(0x200f)/E(0x2010)` | `flush` |
| Other Flush | `1−Nuke Flush` | `flush` |

`Other Branch` 的公式没有再减去 Indirect Branch；这些标签不是互斥分类，不能强求四项加和为 100%。Flush 系数是该平台公式的一部分，不能照搬到 950。

### 3.3 后端、访存与端口

| 指标 | 公式 | 取数组 |
| --- | --- | --- |
| Memory Bound | `(E(0x7005)+E(0x7006))/C` | `backend_memory` |
| Core Bound | `E_backend_core(0x7001)/C_backend_core − Memory Bound` | 跨组 |
| FDIV / DIV / FSU Stall | 分别为 `E(0x7002)/C`、`E(0x7003)/C`、`E(0x7004)/C` | `backend_core` |
| Exe Ports Util | `Core Bound−FDIV−DIV−FSU` | 跨组残差 |
| L1 Bound | `(E(0x7005)−E(0x7007))/C` | `backend_memory` |
| L2 Bound | `(E(0x7007)−E(0x7008))/C` | `backend_memory` |
| L3或DRAM等待 | `E(0x7008)/C` | `backend_memory` |
| Store Bound | `E(0x7006)/C` | `backend_memory` |
| Resource Bound | 不提供有效公式 | — |

920B 的 `0x7008` 在本套汇总中保留为“L3或DRAM等待”。不能用 `0x7009` 强行拆出可信的 L3 Bound 与 DRAM Bound，也不能由该计数为零推导“没有访问 DRAM”。`Exe Ports Util` 是残差字段，不是所有执行端口利用率的直接测量。

扩展组提供以下细分项，每项均为对应事件除以**本组**周期数：

| 组 | 事件与指标 |
| --- | --- |
| `l1_detail_a` | `0x5090` DTLB；`0x5091` Misalign；`0x5092` Resource full；`0x5093` Instruction type |
| `l1_detail_b` | `0x5094` Forward hazard；`0x5095` Structure hazard；`0x5096` Pipeline |
| `ports_a` | `0x700a` 0 Ports Serialize；`0x700b` 0 Ports Non Serialize；`0x700c` 1 Ports；`0x700d` 2 Ports |
| `ports_b` | `0x700e` 3 Ports；`0x700f` 4 Ports；`0x7010` 5 Ports；`0x7011` 6P Ports |

端口计数提供分类线索；“0 Ports Serialize 较高”不能独立证明某条屏障指令导致停顿。需要把热点、指令位置和保持语义的对照实验联系起来。

### 3.4 Cache、TLB 和分支统计

下表每行的 missrate 为 `miss/access`，MPKI 为 `1000×miss/I`，分子分母必须来自相同组和有效行。

| 指标 | miss | access | 组 |
| --- | --- | --- | --- |
| L1I | `0x0001` | `0x0014` | `icache` |
| L2I | `0x0028` | `0x0027` | `icache` |
| L1D | `0x0003` | `0x0004` | `dcache` |
| L2D | `0x0017` | `0x0016` | 扩展结果取 `levels`；常规结果取 `dcache` |
| L3 | `0x002a` | `0x002b` | `l3` |
| iTLB | `0x0002` | `0x0026` | `tlb1` |
| dTLB | `0x0005` | `0x0025` | `tlb1` |
| sTLB | `0x002d+0x002e` | `0x002f+0x0030` | `tlb2` |
| Branch | `0x0022` | `0x0021` | `branch` |

扩展结果另记录：Mem Access `0x0013`、L1D Writeback `0x0015`、L1D LMiss Read `0x0039`、DTLB Walk `0x0034`、LLC Read `0x0036`、LLC Read Miss `0x0037`、Bus Access `0x0019`、Remote Access Read `0x0038`。单位为**平均计数/调用**，不是百分比、字节数或带宽。

L2 来源细分使用两组：

| 来源 | access | hit | 计算 |
| --- | --- | --- | --- |
| LSU | `0x6008` | `0x600b` | `hit/access`，推导 missrate 为 `1−hit/access` |
| IFU | `0x6009` | `0x600c` | 同上 |
| MMU | `0x600a` | `0x600d` | 同上 |
| Device Store / NC Write | `0x6010` / `0x6011` | — | 平均计数/调用 |

这是访问来源与命中统计，不能替代周期停顿指标 `L2 Bound`。配套扩展验证／导出器负责这些附加列，普通汇总器未生成该列不代表原始事件未采到。

### 3.5 指令混合

`dp_spec=E_imix(0x0073)/S_imix`；`ld_spec=E_imix(0x0070)/S_imix`；`st_spec=E_imix2(0x0071)/S_imix2`；`ase_spec=E_imix(0x8005)/S_imix`。

`branch_spec=E_imix(0x0078)/S_imix + [E_imix2(0x0079)+E_imix2(0x007a)]/S_imix2`。后者跨两个独立轮次，不应当作单次执行的精确指令分类。

## 4. 950 公式差异

950 的一级宽度为 8。缓存、TLB、IPC 和上述指令混合公式使用其对应配置；以下项目必须换用 950 语义。

| 指标 | 950 公式／取数组 |
| --- | --- |
| Retire | `I/(8C)`，`topdown` |
| FrontendBound | `E(0x1f21)/(8C)`，`topdown` |
| Fetch Latency Bound | `E(0x1f22)/C`，`topdown` |
| Fetch Bandwidth Bound | `FrontendBound−Fetch Latency Bound` |
| BadSpec / BackendBound | `(S−I)/(8C)` / `1−Retire−FrontendBound−BadSpec` |
| Idle by iTLB / iCache Miss | `E(0x1f10)/C` / `E(0x1f11)/C`，`frontend_detail` |
| OoO / SP / Branch Flush | `E(0x1f12)/C` / `E(0x1f13)/C` / `E(0x1f14)/C`，`frontend_detail` |
| Flush | 上述三项相加，不乘 920B 的 9/5 系数 |
| Branch Mispredicts | `BadSpec×E(0x0010)/(E(0x0010)+E(0x2010))`，比例取 `badspec_branch` |
| Machine Clears | `BadSpec−Branch Mispredicts` |
| Nuke Flush | `E(0x203f)/E(0x2040)`，`branch` |
| Resource Bound | `E(0x7000)/C`，`backend_core` |
| Exe Ports Util | `Core Bound−Resource Bound−FDIV−DIV−FSU` |
| L3 Bound | `(E(0x7008)−E(0x7009))/C`，`backend_memory` |
| Mem Bound | `E(0x7009)/C`，`backend_memory` |

Memory Bound、Core Bound、L1 Bound、L2 Bound、Store Bound 的形式与第 3 节相同，但使用 950 对应事件组。间接分支、Push、Pop、Other Branch 和 Other Flush 的形式相同；不因此获得与 920B 完全等价的微架构含义。

## 5. Hygon 指标

Hygon 配置为 11 个 Core PMU 组加 1 个 L3 uncore 组。报告中的 Topdown 是代理模型；若平台事件只依据候选微架构定义映射，实际读到计数仍不足以完成该事件语义的硬件验证。

令 `C=cycles(0x76)`、`I=instructions(0xc0)`、`U=retired_uops(0xc1)`、`D=dispatched_uops(0x03aa)`，一级计算为：

| 指标 | 公式 | 解释 |
| --- | --- | --- |
| IPC | `I/C` | 指令/周期 |
| Retire | `U/(6C)` | 使用退休 uop，不是 `I/(6C)` |
| FrontendBound | `frontend_stall_cycles(0x0487)/C` | 前端代理比例，不能进一步归因为 iTLB |
| BadSpec | `max(D−U,0)/(6C)` | 非退休分发 uop 代理 |
| BackendBound | `1−Retire−FrontendBound−BadSpec` | 残差 |

汇总器在前三项之和大于 1.05 时把一级四项全部标为无效。该门槛是质量规则，不是硬件保证；小幅负残差也不应被改写成零来掩盖不闭合。

| 指标 | 公式／注意事项 |
| --- | --- |
| Branch missrate / MPKI | `0xc3/0xc2` / `1000×0xc3/0xc0`，`branch` |
| L1I missrate / MPKI | `0x81/0x80` / `1000×0x81/0xc0`，按 fetch/miss window 计数 |
| L2I missrate / MPKI | `0x0164/0x0764` / `1000×0x0164/0xc0` |
| L1D missrate / MPKI | `0xc860/0x0729` / `1000×0xc860/0xc0`；分子为 miss proxy |
| L2D missrate / MPKI | `0x0864/(0xf064+0x0864)` / `1000×0x0864/0xc0` |
| dTLB missrate / MPKI | `0xff45/0x0729` / `1000×0xff45/0xc0` |
| sTLB missrate / MPKI | `0xf045/(0x0f45+0xf045)` / `1000×0xf045/0xc0` |
| L3 missrate | `amd_l3:0x0106 / amd_l3:0xff04`，共享域统计 |
| L3 MPKI | `1000×ΣL3 misses/ΣCore instructions`，跨组、跨统计范围，只能作为带说明的代理值 |
| Indirect Branch | `0xca/0xc3`，占全部分支误预测的比例 |
| Pop Branch | `0xc9/0xc3`；与 return missrate `0xc9/0xc8` 不同 |
| 正式 iTLB missrate / MPKI | 不支持；不能填零，候选诊断事件不能直接补成正式列 |

`ld_spec=load_ops/D`，`st_spec=(store_ops+load_store_ops)/D`，`ase_spec=fpu_spec_uops/D`。`dp_spec` 为扣除 load、store、branch-uop proxy 和 ASE 后的残差，下限为零；组成项之和大于 1.05 时相关项无效。独立显示的 `branch_spec=branches/instructions` 使用另一分母，因此展示列不要求相加为 100%。

前端空队列、反压、除法器 busy、token stall 和 L2 fill pending 等诊断项使用各组事件除以该组 cycles。它们可帮助定位，不能自动填进 Arm 的 Core/Memory/L1/L2 Bound 树。未有有效映射的细项保留不支持。

## 6. 事件组清单

公共配置中的 `EVENTS_*` 是事件编码的直接来源；解析列、汇总公式和报告字段应一起核对。

| 平台／流程 | PMU 组 |
| --- | --- |
| 920B 常规，13 组 | `topdown`、`flush`、`badspec_branch`、`backend_core`、`backend_memory`、`icache`、`dcache`、`l3`、`tlb1`、`tlb2`、`branch`、`imix`、`imix2` |
| 920B 扩展，共 22 组 | 上述 13 组，加 `l1_detail_a`、`l1_detail_b`、`ports_a`、`ports_b`、`cache`、`trans`、`levels`、`l2_lsu_ifu`、`l2_mmu_device` |
| 950，13 组 | 将 920B 常规的 `flush` 换为 `frontend_detail`，其余组名相同，事件编码使用 950 配置 |
| Hygon，12 组 | `topdown`、`branch`、`spec_ls`、`spec_ase`、`icache`、`dcache`、`tlb`、`branch_detail`、`frontend_detail`、`backend_detail`、`memory_detail`、`l3` |

`time`、热点、频率、SPE 和干净 Benchmark 不计入 PMU 组数。组数一致只证明目录覆盖，还需逐组检查事件列、阶段数和有效记录。

## 7. 三种时间和频率

### 7.1 阶段 wall time 与 thread CPU time

`kperf_instrument.py` 用 `perf_counter_ns()` 读取 wall time，用 `thread_time_ns()` 读取当前线程 CPU time。初始化时进行 257 次空边界校准，取低位中位数作为各自开销：

```text
wall_ns   = max(0, wall_end − wall_start − wall_overhead)
thread_ns = max(0, thread_end − thread_start − thread_overhead)
time_us   = mean(wall_ns) / 1000
CPU利用率 = Σthread_ns / Σwall_ns
```

wall time 包含等待和调度间隔；thread time 仅累计该线程获得 CPU 执行的时间。这里的 CPU 利用率不是整机、进程全部线程或 GPU 利用率。过短区间会受边界开销和时钟分辨率影响；扣除后 wall time 为零的记录无效。

### 7.2 报表频率与独立频率观察

基础报表的 `频率(MHz)=mean(cycles)/mean(wall_time_us)` 来自独立 PMU 轮和 time 轮。它是跨请求估算，受两轮差异和等待时间影响，不能当作实测 CPU 时钟。

带频率验证的扩展导出器将 `频率(MHz)` 写为独立观察轮的 Worker 频率，并把上述计算另存为 `cycles/time（跨请求近似，不作为实测频率）`。读取结果时先确认字段由哪一种导出器生成。

频率观察轮由独立工具读取频率序列，按角色／CPU 汇总；它仍不对应每一条 PMU 样本的同时刻频率。锁频目标、policy 读回、观察值和该估算字段应分别保存。

### 7.3 请求性能

Benchmark 的 TTFT、TPOT、ITL、请求耗时和吞吐来自客户端请求统计。它们包含调度、服务和通信等开销，与阶段 wall time 的边界不同。完整性能比较使用未打开阶段 PMU、SPE 或 Trace 的干净请求；采集轮中的 Benchmark 用于核查负载和扰动。

## 8. 算子级 ReadCoreCycle

ReadCoreCycle 位于算子插桩代码包，与上述 Topdown 脚本分开。边界同时读取 `PMCCNTR_EL0` 和 `CNTVCT_EL0`，由 `CNTFRQ_EL0` 提供系统计时器频率。当前正式时延按配对计时器转换：

```text
delta_cycles = end_cycles − begin_cycles
delta_ticks  = end_tick − begin_tick
latency_us   = delta_ticks / CNTFRQ × 1,000,000
paired_cycle_hz = delta_cycles / latency_us × 1,000,000
```

输出还可保留 `cpufreq_latency_us=delta_cycles/(cpu_frequency_hz/1e6)` 作为频率换算参考；正式 `latency_us` 不使用固定 CPU 主频代替配对时钟。

### 8.1 采集调用顺序

由实际执行目标算子的 Worker 线程调用控制器。先将 `VLLM_RCC_RUNTIME` 设置为本次 RCC 动态库的绝对路径；控制器要求 ABI 4，插桩点与 Stage ID 必须属于同一构建。最小集成顺序为：

```python
from tools.collect_readcorecycle import RccWorkerExtension

controller = RccWorkerExtension()
controller.rcc_prepare(site_id, stage_id, context_id, capacity)
controller.rcc_set_step(step_id)
controller.rcc_arm()
# 在此线程执行一次选定模型阶段；调用项目自身的模型执行接口。
stop = controller.rcc_stop_dump(raw_csv_path)
```

这里的标识、容量和路径由调用方提供。`rcc_prepare()` 在测量前准备缓冲，`rcc_arm()` 开始记录，`rcc_stop_dump()` 停止后写出。不要把磁盘写入、内存准备或额外 GPU 同步加进待测区间。示例展示接口顺序，不是独立推理程序。

配套 `run_qwen3_rcc_measurement.py` 是模型驱动器，使用环境变量读取以下控制项。模型构造与运行放置还需按实际应用适配；它不是任意模型通用启动器。

| 参数 | 含义 |
| --- | --- |
| `RCC_REPO` / `RCC_RUN_DIR` | 插桩代码根目录／本轮输出目录 |
| `RCC_PHASE` | `eager-prefill`、`eager-decode`、`eager-logits`、`graph-capture-full`、`graph-capture-piecewise`、`graph-prefill`、`graph-replay`、`graph-outside`、`graph-logits` |
| `RCC_STAGE_ID` / `RCC_CONTEXT_ID` | 本轮精确选择的 Stage／上下文 |
| `RCC_CAPACITY` | 记录容量，1–4096；至少覆盖预期记录数 |
| `RCC_EXPECTED_COUNT` | 应命中的次数；用于检查覆盖 |
| `RCC_WINDOW_SCOPE` | `single-step` 或 `full-request` |
| `RCC_TARGET_STEP` | 目标 Decode step；与 Prefill 编号区分 |
| `RCC_INPUT_TOKENS` / `RCC_MAX_TOKENS` | 请求长度 |
| `RCC_KV_CACHE_MEMORY_BYTES` | KV cache 预算，字节 |
| `RCC_CPU_FREQUENCY_HZ` | 必填正整数；供参考转换，不替代 CNTFRQ |
| `RCC_INSTRUMENTATION_ACTIVE` | `1` 记录，`0` 用于关闭记录的对照 |

### 8.2 离线解析

在插桩代码根目录执行，参数来自本轮原始记录和控制器返回值：

```bash
"$PYTHON_BIN" tools/collect_readcorecycle.py \
  --raw "$RAW_CSV" --summary "$SUMMARY_JSON" --derived "$DERIVED_CSV" \
  --cpu-frequency-hz "$CPU_FREQUENCY_HZ" --pmcr-divider "$PMCR_DIVIDER" \
  --selected-count "$SELECTED_COUNT" --event-count "$EVENT_COUNT" \
  --lost-count "$LOST_COUNT" --phase "$PHASE" --step "$STEP"
```

核验 `PMCR_EL0.D=0`、`lost_count=0`、计数差值非负、配对 tick 有效、单轮 CNTFRQ 一致、实际 CPU/TID 不漂移，以及选中数与原始记录一致。不能只检查导出的平均数。

每轮精确选择一个 Stage，独立重复后先得到每次重复的统计，再汇总重复间波动。嵌套父子 Stage 的独立采样仍不可相减。Graph Replay 只报告整个 `CUDAGraph.replay → cudaGraphLaunch` Host 路径，不能分配成各语义算子的 Replay 时延；GPU 完成时间需要单独测量。

## 参考

- [Linux arm64 perf 支持](https://docs.kernel.org/arch/arm64/perf.html)：平台支持和计数器使用背景。
- 公式核查点：`scripts/config.env`、`scripts/<CHIP>/summary.py`、`scripts/<CHIP>/report_config.json`、扩展采集的验证／导出器。
- 计时核查点：`kperf_instrument.py`、`scripts/parse_run.py`；算子代码包中的 `csrc/read_core_cycle.h`、`tools/collect_readcorecycle.py`、`tools/run_qwen3_rcc_measurement.py`。
