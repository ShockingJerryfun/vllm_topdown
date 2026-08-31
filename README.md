<!-- markdownlint-disable MD001 MD041 -->

# 本分支：vLLM 0.26.0 默认V2 Decode八阶段PMU打点

当前基线为Qwen3-8B、Python 3.13、PyTorch 2.11.0。分支按真实decode串行路径
采集下面八个CPU PMU区间：

| 阶段 | 打点函数 |
| --- | --- |
| `add_requests` | `GPUModelRunner.add_requests()` |
| `prepare_inputs` | `GPUModelRunner.prepare_inputs()` |
| `prepare_attn_runner` | `GPUModelRunner.prepare_attn()` |
| `prepare_attn_model_state` | `DefaultModelState.prepare_attn()` |
| `run_fullgraph` | `ModelCudaGraphManager.run_fullgraph()` |
| `sample` | `GPUModelRunner.sample()` |
| `async_output_init` | `AsyncOutput.__init__()` |
| `postprocess_sampled` | `GPUModelRunner.postprocess_sampled()` |

## 打点工具原理

### 运行时注入与打点边界

芯片入口脚本不会改写容器已安装的vLLM。它先用软链接构造临时
`PYTHONPATH`：未修改文件继续指向容器中的vLLM，当前仓库文件覆盖同名路径，
并把根目录的 `kperf_instrument.py` 加入该运行时。服务固定设置
`VLLM_USE_V2_MODEL_RUNNER=1`，因此实际导入的是本分支带打点的V2代码。
`runtime.txt` 记录Python、PyTorch、vLLM版本以及关键模块的真实导入路径，用于
确认这次运行是否确实命中了该覆盖层。

八个外层函数都在进入原函数主体前调用 `kperf_begin(stage)`，并在 `finally`
中调用 `kperf_finish(stage)`，因此正常返回和异常退出都会尝试关闭区间。探针用
模块级 `ACTIVE` 状态保存当前区间，不是线程局部栈；本方案依赖八段在同一执行
链中串行且不嵌套，不支持把它当作可嵌套或多线程并发的通用Profiler。

### Core PMU计数

PMU轮次不调用 `perf stat`。`kperf_instrument.py` 通过
`perf_event_open(2)` 打开 `PERF_TYPE_RAW` 原始事件：第一个事件是带
`disabled+pinned` 的group leader，其余事件通过 `group_fd` 加入同组；读取格式
同时请求group值、`time_enabled`、`time_running` 和事件ID。

Core PMU在模块初始化时使用 `pid=0, cpu=-1` 打开，绑定到**当时调用
`perf_event_open` 的线程**，并可随该线程迁移CPU；代码没有设置 `inherit`，
因此不会自动统计子线程或子进程。属性只排除Hypervisor，未排除用户态或内核态。
每次 `kperf_begin` 对leader执行整组 `RESET`、`ENABLE`，每次
`kperf_finish` 执行整组 `DISABLE`，然后从leader一次读取整组计数，并按事件ID
还原为配置顺序。

脚本不做multiplex缩放。只有每个组都满足 `time_running > 0` 且
`time_running == time_enabled` 时，该行才标记为有效；初始化、enable、disable或
读取失败会写入失败日志或无效行，且不会形成可进入汇总的有效数据。

Hygon的Core事件同样按上述线程范围采集；`amd_l3` 例外。L3轮次读取
`/sys/bus/event_source/devices/amd_l3/{type,cpumask}`，在cpumask列出的每个
代表CPU上以 `pid=-1` 打开一组system-wide Uncore事件，最后把各L3域的计数
相加。因此Hygon L3是共享域在 `begin/finish` 区间内的活动，不是严格的单线程归因。

### 时间测量

time轮次不打开PMU。每段同时读取：

- `time.perf_counter_ns()`：单调墙钟时间；
- `time.thread_time_ns()`：执行该打点代码的当前线程CPU时间。

模块初始化时做257次空读，分别取墙钟和线程CPU时间开销的低中位数；每条记录
从原始差值中扣除对应开销，并把负值截为0。`CPU利用率` 最终按所有有效Decode
行的 `SUM(thread CPU time) / SUM(wall time)` 计算，不做0%到100%的裁剪。

time和PMU是两类独立采集轮次。芯片脚本先运行一次time，再为每个事件组分别
重启服务、重新发送相同参数的请求；所以表中的时间和cycles不是同一次函数调用的
原子配对值。明细表只是按各阶段在独立轮次中的序号展示时间，汇总中的
`频率(MHz)=平均cycles/平均time(us)` 也只能视为跨轮次估算。

### Decode对齐与质量门槛

探针把记录以 `KPERF_TIME,...` 或 `KPERF,...` 行写入服务标准输出。
`run_one.sh` 只截取服务健康检查完成之后、benchmark结束之前的日志作为
`measurement.log`。`parse_run.py` 先保留各阶段全部raw记录，再按全局调用号排序，
以 `run_fullgraph` 为锚点寻找严格连续的八段窗口：阶段顺序必须与上表一致，八个
全局调用号也必须连续。只有这些窗口进入 `parsed/` 和汇总；Prefill与非对齐调用
仍保留在 `raw/`。

默认output 100对应99个完整Decode窗口。每个time/PMU组的八个阶段都必须选中
99行、全部有效且没有缺失，否则该轮解析直接失败。汇总比率使用
`SUM(分子)/SUM(分母)`，不是先计算逐行比率再平均；绝对时间和绝对计数使用有效
Decode行的算术平均。

### hotspot与PMU打点的区别

`hotspot` 是另一轮独立运行，此时 `KPERF_ENABLE=0`。脚本只在服务就绪后枚举
当时的服务进程树，并执行
`perf record -e cycles:u -F 999 -g --call-graph dwarf`，生成 `perf.data` 和
`perf report`。它是用户态cycles的999Hz调用栈采样，用于定位CPU热点；不是八段
区间的精确事件计数，也不参与Topdown公式。

以上计数和时间全部属于CPU Host侧。即使 `run_fullgraph` 内部触发
`CUDAGraph.replay()`，这里统计的也是Host线程执行、同步和提交行为，不是GPU
cycles或GPU Kernel执行时长。

完整边界、采集口径和使用方法见
[KPERF_TOPDOWN.md](KPERF_TOPDOWN.md)。

公共采集和Excel生成代码位于根目录 `kperf_instrument.py` 和 `scripts/`；
`920b`、`950`、`hygon_c86_7490` 目录只保留各自的事件、公式和配置。
`scripts/run_topdown.sh` 成功结束时会在结果目录直接生成最终工作簿，默认文件名
示例为 `920b_vllm0.26_qwen3_7k100.xlsx`。

---

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/vllm-project/vllm/main/docs/assets/logos/vllm-logo-text-dark.png">
    <img alt="vLLM" src="https://raw.githubusercontent.com/vllm-project/vllm/main/docs/assets/logos/vllm-logo-text-light.png" width=55%>
  </picture>
</p>

<h3 align="center">
Easy, fast, and cheap LLM serving for everyone
</h3>

<p align="center">
| <a href="https://docs.vllm.ai"><b>Documentation</b></a> | <a href="https://blog.vllm.ai/"><b>Blog</b></a> | <a href="https://arxiv.org/abs/2309.06180"><b>Paper</b></a> | <a href="https://x.com/vllm_project"><b>Twitter/X</b></a> | <a href="https://discuss.vllm.ai"><b>User Forum</b></a> | <a href="https://slack.vllm.ai"><b>Developer Slack</b></a> |
</p>

🔥 We have built a vLLM website to help you get started with vLLM. Please visit [vllm.ai](https://vllm.ai) to learn more.
For events, please visit [vllm.ai/events](https://vllm.ai/events) to join us.

---

## About

vLLM is a fast and easy-to-use library for LLM inference and serving.

Originally developed in the [Sky Computing Lab](https://sky.cs.berkeley.edu) at UC Berkeley, vLLM has grown into one of the most active open-source AI projects built and maintained by a diverse community of many dozens of academic institutions and companies from over 2000 contributors.

vLLM is fast with:

- State-of-the-art serving throughput
- Efficient management of attention key and value memory with [**PagedAttention**](https://blog.vllm.ai/2023/06/20/vllm.html)
- Continuous batching of incoming requests, chunked prefill, prefix caching
- Fast and flexible model execution with piecewise and full CUDA/HIP graphs
- Quantization: FP8, MXFP8/MXFP4, NVFP4, INT8, INT4, GPTQ/AWQ, GGUF, compressed-tensors, ModelOpt, TorchAO, and [more](https://docs.vllm.ai/en/latest/features/quantization/index.html)
- Optimized attention kernels including FlashAttention, FlashInfer, TRTLLM-GEN, FlashMLA, and Triton
- Optimized GEMM/MoE kernels for various precisions using CUTLASS, TRTLLM-GEN, CuTeDSL
- Speculative decoding including n-gram, suffix, EAGLE, DFlash
- Automatic kernel generation and graph-level transformations using torch.compile
- Disaggregated prefill, decode, and encode

vLLM is flexible and easy to use with:

- Seamless integration with popular Hugging Face models
- High-throughput serving with various decoding algorithms, including *parallel sampling*, *beam search*, and more
- Tensor, pipeline, data, expert, and context parallelism for distributed inference
- Streaming outputs
- Generation of structured outputs using xgrammar or guidance
- Tool calling and reasoning parsers
- OpenAI-compatible API server, plus Anthropic Messages API and gRPC support
- Efficient multi-LoRA support for dense and MoE layers
- Support for NVIDIA GPUs, AMD GPUs, and x86/ARM/PowerPC CPUs. Additionally, diverse hardware plugins such as Google TPUs, Intel Gaudi, IBM Spyre, Huawei Ascend, Rebellions NPU, Apple Silicon, MetaX GPU, and more.

vLLM seamlessly supports 200+ model architectures on Hugging Face, including:

- Decoder-only LLMs (e.g., Llama, Qwen, Gemma)
- Mixture-of-Expert LLMs (e.g., Mixtral, DeepSeek-V3, Qwen-MoE, GPT-OSS)
- Hybrid attention and state-space models (e.g., Mamba, Qwen3.5)
- Multi-modal models (e.g., LLaVA, Qwen-VL, Pixtral)
- Embedding and retrieval models (e.g., E5-Mistral, GTE, ColBERT)
- Reward and classification models (e.g., Qwen-Math)

Find the full list of supported models [here](https://docs.vllm.ai/en/latest/models/supported_models.html).

## Getting Started

Install vLLM with [`uv`](https://docs.astral.sh/uv/) (recommended) or `pip`:

```bash
uv pip install vllm
```

Or [build from source](https://docs.vllm.ai/en/latest/getting_started/installation/gpu/index.html#build-wheel-from-source) for development.

Visit our [documentation](https://docs.vllm.ai/en/latest/) to learn more.

- [Installation](https://docs.vllm.ai/en/latest/getting_started/installation.html)
- [Quickstart](https://docs.vllm.ai/en/latest/getting_started/quickstart.html)
- [List of Supported Models](https://docs.vllm.ai/en/latest/models/supported_models.html)

## Contributing

We welcome and value any contributions and collaborations.
Please check out [Contributing to vLLM](https://docs.vllm.ai/en/latest/contributing/index.html) for how to get involved.

## Citation

If you use vLLM for your research, please cite our [paper](https://arxiv.org/abs/2309.06180):

```bibtex
@inproceedings{kwon2023efficient,
  title={Efficient Memory Management for Large Language Model Serving with PagedAttention},
  author={Woosuk Kwon and Zhuohan Li and Siyuan Zhuang and Ying Sheng and Lianmin Zheng and Cody Hao Yu and Joseph E. Gonzalez and Hao Zhang and Ion Stoica},
  booktitle={Proceedings of the ACM SIGOPS 29th Symposium on Operating Systems Principles},
  year={2023}
}
```

## Contact Us

<!-- --8<-- [start:contact-us] -->
- For technical questions and feature requests, please use GitHub [Issues](https://github.com/vllm-project/vllm/issues)
- For discussing with fellow users, please use the [vLLM Forum](https://discuss.vllm.ai)
- For coordinating contributions and development, please use [Slack](https://slack.vllm.ai)
- For security disclosures, please use GitHub's [Security Advisories](https://github.com/vllm-project/vllm/security/advisories) feature
- For collaborations and partnerships, please contact us at [collaboration@vllm.ai](mailto:collaboration@vllm.ai)
<!-- --8<-- [end:contact-us] -->

## Media Kit

- If you wish to use vLLM's logo, please refer to [our media kit repo](https://github.com/vllm-project/media-kit)
