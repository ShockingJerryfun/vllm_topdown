# vLLM 0.26.0 默认 V2 六阶段 PMU 打点

本文描述 `vllm_0.26_perf` 当前代码中的实际探针。该分支使用0.26的默认V2
runner：`vllm/v1/worker/gpu/model_runner.py`，不使用0.11的
`vllm/v1/worker/gpu_model_runner.py`。

## 打点本质

`kperf_instrument.py` 通过 Linux `perf_event_open(2)` 为当前执行线程打开
`PERF_TYPE_RAW` PMU事件。它不启动 `perf stat` 子进程，也不是GPU计数器：

1. `kperf_begin(stage)` 对每个事件FD执行 reset、enable，并记录开始时间。
2. 被测代码在原位置执行。
3. `kperf_finish(stage)` 在 `finally` 中执行 disable、read。
4. 每次输出一行
   `阶段,调用序号,时间(us),事件1计数,事件2计数,...`。

已有完整函数使用“外层函数加探针、原函数体移入 `*_inner()`”的形式：

```python
kperf_begin("stage")
try:
    return stage_inner(...)
finally:
    kperf_finish("stage")
```

0.26中有三个阶段原来不是独立的完整区间，因此按同一概念边界抽取：
`update_states` 从 `execute_model()` 开头的状态操作抽出，`sample` 只抽取
采样器或拒绝采样器，`bookkeeping` 从采样后的worker处理尾段抽出。没有把这
三个区间扩大到相邻阶段。

这些计数是CPU PMU计数。尤其是 `forward`，结果表示Python/C++主机线程进行
模型调用、CUDA提交以及同步等待时消耗的CPU事件，不表示A100内部的GPU cycles。
热点采集则是另一条独立流程：`scripts/run_one.sh` 在 `KPERF_ENABLE=0`
时调用 `perf record`，生成 `perf.data` 和 `perf_report.txt`。

## 六阶段实际位置和边界

以下行号以当前 `vllm_0.26_perf` 分支为准。

| 阶段 | 探针和被测函数 | 标准Qwen2.5调用位置 | 本阶段做什么 |
| --- | --- | --- | --- |
| `update_states` | `vllm/v1/worker/gpu/model_runner.py:875` `_update_states()`；实际内容在第882行 `_update_states_inner()` | `execute_model()` 第1206行 | 根据scheduler输出完成/释放请求，加入新请求，更新运行中请求，并提交block table的暂存写入 |
| `prepare_inputs` | `model_runner.py:890` `prepare_inputs()`；实际内容在第899行 `_prepare_inputs_inner()` | `execute_model()` 第1251行 | 排列本轮请求，计算token数量、position、logits索引和spec-decode信息，并准备/拷贝模型输入buffer |
| `forward` | `vllm/model_executor/models/qwen2.py:485` `Qwen2ForCausalLM.forward()`；实际内容在第503行 `_forward_inner()` | eager测试路径由runner第1401行 `self.model(**model_inputs)` 进入 | 执行Qwen2主体网络，从token/position得到hidden states；包含embedding、Transformer层、attention和MLP，不包含LM head、采样与后处理 |
| `compute_logits` | `qwen2.py:515` `Qwen2ForCausalLM.compute_logits()`；实际内容在第525行 `_compute_logits_inner()` | runner `sample()` 第1117行 | 对待采样hidden states执行LM head和logits processor，得到词表logits |
| `sample` | `model_runner.py:1132` `_sample()`；实际内容在第1143行 `_sample_inner()` | 外层 `sample()` 第1128行 | 普通路径调用sampler选择token；spec decode路径调用rejection sampler决定接受/拒绝的token |
| `bookkeeping` | `model_runner.py:1498` `_bookkeeping()`；实际内容在第1526行 `_bookkeeping_inner()` | `execute_model()` 第1486行 | 处理prompt logprobs、构造输出、发起异步D2H拷贝、更新请求状态、可选生成draft token并执行KV connector后处理 |

六阶段在本次普通文本生成路径中的顺序是：

```text
execute_model()
  update_states
  prepare_inputs
  forward
  sample()
    compute_logits
    grammar处理（不在六阶段区间内）
    sample
  bookkeeping
```

具体边界如下：

- `prepare_inputs` 不包含紧随其后的 `prepare_attn()`、model state预处理和
  attention metadata后续准备。
- `forward` 和 `compute_logits` 直接打在Qwen2模型实现中，因此只覆盖经过
  `Qwen2ForCausalLM` 的模型；其他模型架构不会自动获得这两个探针。
- `sample` 不包含第1117行的 `compute_logits`，也不包含第1118至1126行的
  grammar bitmask处理。
- `bookkeeping` 包含 `AsyncOutput` 的创建和异步拷贝发起，但不包含
  `vllm/v1/worker/gpu/async_utils.py:49` 的 `AsyncOutput.get_output()`，
  因而不包含之后等待copy event和将结果整理为Python列表的时间。

## 与0.11打点的区别

| 对比项 | vLLM 0.11.2 V1 | vLLM 0.26.0 默认V2 |
| --- | --- | --- |
| runner文件 | `vllm/v1/worker/gpu_model_runner.py` | `vllm/v1/worker/gpu/model_runner.py` |
| 状态更新 | 包裹原有 `_update_states()` | 将V2 `execute_model()` 内六项状态操作抽成新的 `_update_states()` 区间 |
| 输入准备 | 包裹 `_prepare_inputs()` | 包裹V2 `prepare_inputs()`；不包含独立的 `prepare_attn()` |
| forward | 在runner的 `_model_forward()` 外层打点，适用于该runner调用的模型 | 直接在 `Qwen2ForCausalLM.forward()` 打点，边界更贴近Qwen2模型主体 |
| compute_logits | 在 `Qwen2ForCausalLM.compute_logits()` 打点 | 同样在Qwen2模型实现打点，但调用已移入V2 runner的 `sample()` |
| sample | `sample_tokens()` 中的 `_sample()` | 从V2 `sample()` 中单独抽出sampler/rejection-sampler区间 |
| bookkeeping | 同步 `_bookkeeping_sync()` | worker侧 `_bookkeeping()`，发起异步输出复制，但不等待 `get_output()` |

两个版本对齐的是“推理阶段的概念边界”，不是强求同名函数或相同行号。0.26的
`compute_logits` 位于外层 `sample()` 中，但 `sample` 探针从
`self._sample()` 才开始，所以普通路径下两者不嵌套。

## 适用范围和嵌套限制

`kperf_instrument.py` 只有一组全局 `START_NS`、`NAME` 和FD，不维护调用栈，
因此不支持探针嵌套。当前输入7000、输出100、未请求prompt logprobs的Qwen2.5
测试中，六阶段按上面的顺序执行，没有相互嵌套。

如果请求开启 `prompt_logprobs`，`_bookkeeping_inner()` 第1539至1546行会把
`self.model.compute_logits` 传给prompt-logprobs worker，后者可能在
`bookkeeping` 区间内再次进入 `compute_logits`。这种工作负载不能直接使用
当前单全局状态探针，必须先改成可嵌套采集或关闭prompt logprobs。

## 测试脚本和输出

`scripts/920b/run.sh` 使用 `VLLM_USE_V2_MODEL_RUNNER=1` 和
`--enforce-eager`，按920B事件组重启服务，并依次调用 `run_one.sh`、
`parse_run.py` 和 `summary.py`。当前固定测试参数是Qwen2.5-1.5B、输入7000、
输出100、并发1、TP=1、BF16。

解析时仅在统计口径中排除每阶段第一条prefill，并排除未与其余阶段对齐的
`update_states` 尾部记录；原始记录仍保留。结果目录是
`/home/fj/vllm_v1_six_stage/results/v026`：

- `summary.csv`：六阶段汇总。
- 各事件组目录：原始日志和解析后的阶段数据。
- `hotspot/perf.data`、`hotspot/perf_report.txt`：未做阶段归并的原始热点结果。
