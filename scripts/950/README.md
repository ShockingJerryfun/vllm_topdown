# 鲲鹏950八阶段采集

本目录沿用公共的 `kperf_instrument.py`、`scripts/run_one.sh`和
`scripts/parse_run.py`，只定义950事件组与汇总公式。每组最多6个事件，
与openEuler libkperf中HIPG每组最多6个事件的限制一致。
time使用不打开PMU的独立轮次；汇总指标行与920B模板完全一致，没有采集到
等价事件的指标显示“未采集”。`CPU利用率` 为当前线程CPU时间除以墙钟时间，
脚本不裁剪实测结果。

| 组 | 事件 |
| --- | --- |
| topdown | `0x0011` cycles，`0x0008` inst_retired，`0x1f21` 模板前端事件，`0x001b` inst_spec |
| icache | `0x0008,0x0001,0x0014,0x0027,0x0028` |
| dcache | `0x0008,0x0003,0x0004,0x0017,0x0016` |
| l3 | `0x0008,0x002a,0x002b` |
| tlb1 | `0x0008,0x0002,0x0026,0x0005,0x0025` |
| tlb2 | `0x0008,0x002d,0x002e,0x002f,0x0030` |
| branch | `0x0008,0x0021,0x0022` |
| imix | `0x001b,0x0070,0x0073,0x8005,0x0078` |
| imix2 | `0x001b,0x0071,0x0079,0x007a,0x0075,0x8006` |

Topdown按现有950模板计算：

- `Retire = inst_retired / (8 * cycles)`
- `FrontendBound = 0x1f21 / (8 * cycles)`
- `BadSpec = (inst_spec - inst_retired) / (8 * cycles)`
- `BackendBound = 1 - Retire - FrontendBound - BadSpec`

cache、TLB和branch的miss rate为 `miss / access`，MPKI为
`miss / inst_retired * 1000`；IMIX为对应事件除以 `inst_spec`。

Arm架构事件号已与openEuler内核PMU事件表核对。`0x1f21`、
`0x8005`、`0x8006`和8 slots/cycle是实现相关定义，当前只有现有
950模板依据；需要在950真机上确认事件可打开、计数非零，并与
Kunpeng DevKit的Topdown结果交叉检查。汇总脚本不裁剪或强行归一化
异常值，便于真机验证事件语义。

核对依据：

- [openEuler libkperf适配950](https://gitee.com/openeuler/libkperf/commit/eafb7506e39fac4c69cdb7f1b2693568f8883b30)
- [openEuler Arm64 PMU架构事件表](https://gitee.com/openeuler/kernel/blob/OLK-6.6/tools/perf/pmu-events/arch/arm64/common-and-microarch.json)
- [Kunpeng DevKit Topdown指标定义](https://www.hikunpeng.com/document/detail/en/kunpengdevps/userguide/cliuserguide/KunpengDevKitCli_0254.html)

容器内先安装一次报表依赖：

```bash
/opt/vllm/bin/python3 -m pip install \
  -r /home/fj/vllm_fj/scripts/requirements-report.txt
```

宿主机执行：

```bash
CHIP=950 bash /home/fj/vllm_fj/scripts/run_topdown.sh
```

运行前根据实际容器、项目和模型路径设置 `CONTAINER`、`PROJECT`和
`MODEL`。输出位于 `results/950/<RUN_ID>`，默认最终工作簿为
`950_vllm0.26_qwen3_7k100.xlsx`。
