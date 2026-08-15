# vLLM 0.26.0 默认 V2 六阶段采集

0.26 默认路径使用 `vllm/v1/worker/gpu/model_runner.py`，不复用旧 `gpu_model_runner.py`。打点函数保留原函数名作为外层入口，原函数体移动到对应的 `*_inner()`，外层只执行 `kperf_begin()`、内部调用和 `finally` 中的 `kperf_finish()`。

| 阶段 | 打点位置 | 实际调用位置 |
| --- | --- | --- |
| `update_states` | `vllm/v1/worker/gpu/model_runner.py:875` `_update_states()` | 同文件 `execute_model()` 第1206行 |
| `prepare_inputs` | 同文件第890行 `prepare_inputs()` | `execute_model()` 第1251行 |
| `forward` | `vllm/model_executor/models/qwen2.py:485` `Qwen2ForCausalLM.forward()` | runner 第1401行 `self.model(...)` |
| `compute_logits` | `qwen2.py:515` `Qwen2ForCausalLM.compute_logits()` | runner `sample()` 第1117行 |
| `sample` | `model_runner.py:1132` `_sample()` | `sample()` 第1128行 |
| `bookkeeping` | `model_runner.py:1498` `_bookkeeping()` | `execute_model()` 第1486行 |

`prepare_inputs` 不包含紧随其后的 `prepare_attn()`；`sample` 不包含 logits 与 grammar 处理；`bookkeeping` 覆盖 worker 侧采样后处理，但不把异步执行的 `AsyncOutput.get_output()` 合入区间。

`scripts/920b/run.sh` 固定使用 `VLLM_USE_V2_MODEL_RUNNER=1` 和 eager，按事件组重启服务并调用 `run_one.sh`、`parse_run.py` 与 `summary.py`。模型参数固定为输入7000、输出100、并发1、TP=1、BF16。解析时排除每阶段第一条 prefill，并额外排除 `update_states` 最后一条尾部异常。

运行结果写到 `/home/fj/vllm_v1_six_stage/results/v026`。`summary.csv` 是六阶段汇总；`hotspot/perf.data` 和 `hotspot/perf_report.txt` 是未做阶段归并的原始 perf 热点结果。
