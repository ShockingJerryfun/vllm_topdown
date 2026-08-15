# vLLM 0.26.0 default V2 runner kperf probes

Base source: upstream tag `v0.26.0`, commit
`568afb3a13806beb53bb2e6bd518269357b237c0`.

This branch instruments the default V2 GPU model runner. It does not reuse or
modify the legacy `vllm/v1/worker/gpu_model_runner.py` probe implementation.

| Stage | Probe boundary | Source file |
| --- | --- | --- |
| `update_states` | Composite request-state update group called at the start of `execute_model()` | `vllm/v1/worker/gpu/model_runner.py` |
| `prepare_inputs` | `GPUModelRunner.prepare_inputs()`; `prepare_attn()` remains outside | `vllm/v1/worker/gpu/model_runner.py` |
| `forward` | `Qwen2ForCausalLM.forward()` on the eager Qwen2.5 path | `vllm/model_executor/models/qwen2.py` |
| `compute_logits` | `Qwen2ForCausalLM.compute_logits()` | `vllm/model_executor/models/qwen2.py` |
| `sample` | Sampler or rejection-sampler branch after logits and grammar processing | `vllm/v1/worker/gpu/model_runner.py` |
| `bookkeeping` | Post-sample prompt-logprob, async-output creation, state update, speculation, and KV post-forward region | `vllm/v1/worker/gpu/model_runner.py` |

Each stage is represented by one wrapper and one `*_inner()` implementation.
The bookkeeping interval stays inside the worker-side post-sample region.
`AsyncOutput.get_output()`, which may execute later or on another thread, is
not folded into this interval because doing so would change the default async
execution boundary.

The scripts explicitly set `VLLM_USE_V2_MODEL_RUNNER=1` and
`--enforce-eager`. Eager mode ensures that the Qwen2 `forward()` wrapper is
executed rather than bypassed by a CUDA graph replay.

The collection entrypoints are:

- `scripts/run_one.sh`
- `scripts/run_all.sh`
- `scripts/parse_run.py`

Required runtime values include `MODEL`, `VLLM_BIN`, and the source or
editable-install path in `VLLM_PYTHONPATH`.

