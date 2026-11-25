# 鲲鹏950八阶段采集

本目录沿用公共的 `kperf_instrument.py`、`scripts/run_one.sh`和
`scripts/parse_run.py`，只定义950事件组与汇总公式。每组最多6个事件，
与openEuler libkperf中HIPG每组最多6个事件的限制一致。
time使用不打开PMU的独立轮次；汇总指标行与920B模板完全一致，没有采集到
等价事件的指标显示“未采集”。`CPU利用率` 为当前线程CPU时间除以墙钟时间，
脚本不裁剪实测结果。`频率(MHz)` 为平均cycles除以独立time轮次的平均
`time(us)`，仅作为跨轮次估算值。

| 组 | 事件 |
| --- | --- |
| topdown | `0x0011` cycles，`0x0008` inst_retired，`0x1f21` fetch bubble，`0x1f22` full-width fetch bubble，`0x001b` inst_spec |
| frontend_detail | `0x0011` cycles，`0x1f10` iTLB miss bubble，`0x1f11` iCache miss bubble，`0x1f12` OoO flush bubble，`0x1f13` static predictor flush bubble，`0x1f14` branch-unit flush bubble |
| badspec_branch | `0x0010` branch mispredict，`0x2010` ROB flush，`0x1010` indirect，`0x1013` BLR，`0x1016` BL，`0x100d` return |
| backend_core | `0x0011` cycles，`0x7000` resource stall，`0x7001` execution stall，`0x7002` FDIV/FSQRT stall，`0x7003` DIV stall，`0x7004` FSU stall |
| backend_memory | `0x0011` cycles，`0x7005` any-load stall，`0x7006` store stall，`0x7007` L1 miss stall，`0x7008` L2 miss stall，`0x7009` L3 miss stall |
| icache | `0x0008,0x0001,0x0014,0x0027,0x0028` |
| dcache | `0x0008,0x0003,0x0004,0x0017,0x0016` |
| l3 | `0x0008,0x002a,0x002b` |
| tlb1 | `0x0008,0x0002,0x0026,0x0005,0x0025` |
| tlb2 | `0x0008,0x002d,0x002e,0x002f,0x0030` |
| branch | `0x0008,0x0021,0x0022,0x203f,0x2040` |
| imix | `0x001b,0x0070,0x0073,0x8005,0x0078` |
| imix2 | `0x001b,0x0071,0x0079,0x007a,0x0075,0x8006` |

Topdown按openEuler HIP12口径计算：

- `Retire = inst_retired / (8 * cycles)`
- `FrontendBound = 0x1f21 / (8 * cycles)`
- `Fetch Latency Bound = 0x1f22 / cycles`
- `Fetch Bandwidth Bound = FrontendBound - Fetch Latency Bound`
- `Idle by iTLB Miss = 0x1f10 / cycles`
- `Idle by iCache Miss = 0x1f11 / cycles`
- `OoO/SP/Branch Flush = 0x1f12/0x1f13/0x1f14 / cycles`
- `Flush = OoO Flush + SP Flush + Branch Flush`
- `BadSpec = (inst_spec - inst_retired) / (8 * cycles)`
- `Branch Mispredicts = BadSpec * 0x0010 / (0x0010 + 0x2010)`
- `Machine Clears = BadSpec - Branch Mispredicts`
- `Nuke Flush = 0x203f / 0x2040`，`Other Flush = 1 - Nuke Flush`
- `BackendBound = 1 - Retire - FrontendBound - BadSpec`
- `Memory Bound = (0x7005 + 0x7006) / cycles`
- `Core Bound = 0x7001 / cycles - Memory Bound`
- `Resource/FDIV/DIV/FSU = 0x7000/0x7002/0x7003/0x7004 / cycles`
- `Exe Ports Util = Core Bound - Resource Bound - FDIV - DIV - FSU`
- `L1/L2/L3 Bound = (0x7005-0x7007)/(0x7007-0x7008)/(0x7008-0x7009) / cycles`
- `Mem Bound = 0x7009 / cycles`，`Store Bound = 0x7006 / cycles`

cache、TLB和branch的miss rate为 `miss / access`，MPKI为
`miss / inst_retired * 1000`；IMIX为对应事件除以 `inst_spec`。
上述阶段汇总比率统一按 `SUM(分子)/SUM(分母)` 计算。端到端区间与八阶段
独立重复采集，在汇总页位于八阶段之前，不参与 `cycle占比` 求和。

鲲鹏950对应HiSilicon HIP12。前端事件、8 slots/cycle和上述公式来自
openEuler内核HIP12 Topdown事件表；`0x8005`、`0x8006`仍只有现有950模板依据。
首次在950真机采集时仍需确认每组事件可打开、计数非零且
`time_running == time_enabled`。汇总脚本不裁剪或强行归一化异常值。

核对依据：

- [openEuler libkperf适配950](https://gitee.com/openeuler/libkperf/commit/eafb7506e39fac4c69cdb7f1b2693568f8883b30)
- [openEuler HIP12 Topdown事件与公式](https://mailweb.openeuler.org/archives/list/kernel@openeuler.org/message/NOJ5FORC264PO3IJ5YGXNBK3KKSSMUN4/)
- [GCC HIP12补丁标明鲲鹏950](https://gcc.gnu.org/pipermail/gcc-patches/2026-February/707387.html)
- [openEuler Arm64 PMU架构事件表](https://gitee.com/openeuler/kernel/blob/OLK-6.6/tools/perf/pmu-events/arch/arm64/common-and-microarch.json)
- [Kunpeng DevKit Topdown指标定义](https://www.hikunpeng.com/document/detail/en/kunpengdevps/userguide/cliuserguide/KunpengDevKitCli_0254.html)

容器内先安装一次报表依赖：

```bash
/opt/vllm/bin/python3 -m pip install \
  -r /home/fj/vllm_topdown/scripts/requirements-report.txt
```

宿主机执行：

```bash
bash /home/fj/vllm_topdown/scripts/run_topdown.sh
```

运行前在公共的 `scripts/config.env` 中设置 `CHIP=950`，并根据
实际环境修改容器、项目和模型路径。输出位于
`results/950/<RUN_ID>`，默认最终工作簿为
`950_vllm0.26_qwen3_7k100.xlsx`。
