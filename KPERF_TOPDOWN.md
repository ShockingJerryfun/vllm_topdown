# vLLM 0.26.0 默认V2 Decode八阶段PMU采集

本分支只对真实decode路径中的八个串行函数做CPU PMU打点。探针在函数外层
调用 `kperf_begin()`，并在 `finally` 中调用 `kperf_finish()`；原函数主体放在
对应的 `*_inner()` 中，八个统计区间不互相嵌套。

## 阶段边界

| 顺序 | 阶段名 | 外层函数 | 主要内容 |
| --- | --- | --- | --- |
| 1 | `add_requests` | `GPUModelRunner.add_requests()` | 把scheduler的新请求加入GPU侧持久请求状态 |
| 2 | `prepare_inputs` | `GPUModelRunner.prepare_inputs()` | 准备本轮token、position和模型输入buffer |
| 3 | `prepare_attn_runner` | `GPUModelRunner.prepare_attn()` | runner侧attention输入与metadata准备 |
| 4 | `prepare_attn_model_state` | `DefaultModelState.prepare_attn()` | model state侧attention metadata准备 |
| 5 | `run_fullgraph` | `CUDAGraphWrapper.run_fullgraph()` | replay完整CUDA Graph |
| 6 | `sample` | `GPUModelRunner.sample()` | logits处理与GPU采样 |
| 7 | `async_output_init` | `AsyncOutput.__init__()` | 创建异步输出并发起结果回传 |
| 8 | `postprocess_sampled` | `GPUModelRunner.postprocess_sampled()` | 更新请求状态并整理采样结果 |

本次采集配置以 `run_fullgraph` 为八阶段调用对齐锚点，只适用于走完整Graph的
decode轮次；eager和piecewise路径不属于这套八阶段统计口径。所有原始调用仍写入
明细页，第一轮prefill和未对齐调用不会进入汇总值。

这些数据是当前Python/C++执行线程的CPU PMU计数，反映主机调度、CUDA提交和
同步等待等CPU行为，不代表GPU内部cycles或GPU kernel效率。热点函数是独立的
`perf record` 采集，脚本优先把容器内完成符号解析的报告写入工作簿。

## 脚本结构

- `scripts/run_topdown.sh`：宿主机统一入口。
- `scripts/run_one.sh`：容器内启动服务、发送请求并采集单个事件组或热点。
- `scripts/parse_run.py`：保留全部原始记录并标记prefill、decode和未对齐调用。
- `scripts/build_xlsx.py`：公共Excel生成器。
- `scripts/<芯片>/run.sh`：芯片采集流程。
- `scripts/<芯片>/report_config.json`：芯片事件列、公式和工作表配置。

目前支持 `920b`、`950` 和 `hygon_c86_7490`。公共生成逻辑复用，芯片事件和
公式仍由各自目录维护。

## 使用方法

容器内第一次使用时安装唯一的报表依赖：

```bash
/opt/vllm/bin/python3 -m pip install \
  -r /home/fj/vllm_fj/scripts/requirements-report.txt
```

宿主机选择芯片并执行：

```bash
CHIP=920b bash /home/fj/vllm_fj/scripts/run_topdown.sh
CHIP=950 bash /home/fj/vllm_fj/scripts/run_topdown.sh
CHIP=hygon_c86_7490 bash /home/fj/vllm_fj/scripts/run_topdown.sh
```

容器名、项目路径、模型路径不同，直接通过环境变量覆盖：

```bash
CONTAINER=qwen3_container_fj \
PROJECT=/home/fj/vllm_fj \
MODEL=/home/fj/Qwen3-8B \
CHIP=920b \
bash /home/fj/vllm_fj/scripts/run_topdown.sh
```

默认输入7000、输出100、模型简写 `qwen3`、版本简写 `0.26`。最终文件按
“芯片_vLLM版本_模型简写_输入输出”命名，例如：

```text
920b_vllm0.26_qwen3_7k100.xlsx
```

可通过 `MODEL_SHORT` 和 `VLLM_VERSION_SHORT` 修改文件名中的模型与版本。

## Excel内容

- 第一个sheet固定为 `汇总`，只汇总对齐的decode调用；CPU利用率未采集时显示
  `未采集`。
- 第二个sheet固定为 `热点函数`，使用容器内 `perf report` 解析后的报告。
- 其余明细sheet保留全部原始记录，包括prefill、decode和未对齐调用。
- `prepare_attn` 明细包含runner与model state两个区段；`output` 明细包含
  `async_output_init` 与 `postprocess_sampled` 两个区段。
- 普通sheet冻结首行和首列；热点正文保持左对齐。
- 920b和950默认生成56个sheet，Hygon默认生成38个sheet。

每次运行结果位于 `results/<芯片>/<RUN_ID>/`，包含原始日志、解析CSV、
`summary.csv`、`collection_quality.csv`、热点文件和最终Excel。只有最终Excel
存在且非空时，宿主机入口才报告成功。
