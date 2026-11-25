# vLLM Topdown

## 前置环境

- Linux、Docker、NVIDIA 驱动及 NVIDIA Container Toolkit。
- 宿主机 Python 3.11 及 PyYAML；容器镜像内有 vLLM、PyTorch、perf 和报表依赖。
- 已准备镜像和模型，容器可访问 CPU PMU。镜像的 CPU 架构须与宿主机一致。

参考镜像 `fangjin/topdown-920b:0.26-cu130` 包含 AArch64 vLLM 0.26.0；x86 机器使用对应架构的镜像。宿主机需要分别准备模型文件和驱动。

## 运行

复制并修改配置中的镜像、模型、GPU、NUMA 节点和 CPU 编号：

```bash
cp configs/bench/infer/llm/vllm/920b.yaml configs/bench/infer/llm/vllm/local.yaml
python3 common/runners/run.py configs/bench/infer/llm/vllm/local.yaml
```

Zen 4 使用 `zen4.yaml`；950、海光可从 920B 示例调整 `CHIP` 和环境。920B 示例采集父阶段、八阶段、22 组 PMU、热点和 Benchmark；SPE 默认关闭。

脚本创建本次专用容器并在结束后关闭。每次生成独立 `run_id`：

| 目录 | 内容 |
| --- | --- |
| `results/bench/infer/llm/<run_id>/` | Benchmark 和服务日志 |
| `results/collect/pmu/<run_id>/` | PMU 和热点原始记录 |
| `results/collect/time/<run_id>/` | 阶段耗时原始记录 |
| `results/analyze/pmu/<run_id>/` | Topdown 指标、质量记录和 Excel |
| `results/analyze/latency/<run_id>/` | Benchmark 指标 CSV |

Excel 保留原 Topdown、Hotspot 样式；启用 SPE 时保留 SPE 页。各组和阶段来自不同采集轮次，不作为同一时间线相加。

## 其他负载

通用打点入口位于 `collect/pmu/topdown/`，在被测线程使用 `from topdown import measure` 标记区间。将仓库和 `collect/pmu` 加入 `PYTHONPATH`，通过 `python -m topdown` 运行；920B 和 Zen 4 的事件组分别配置。

vLLM 的独立采集适配入口为 `bench/infer/llm/vllm/run_probe.sh`，使用 `configs/bench/infer/llm/vllm/probe.env.example` 配置。
