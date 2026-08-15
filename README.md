# vLLM 0.11.2 V1 kperf probes

This repository contains the Python files used to collect Host PMU data for
Qwen2.5-1.5B on the vLLM 0.11.2 V1 path.

## Probe boundaries

| Stage | Instrumented function | File |
| --- | --- | --- |
| `update_states` | `GPUModelRunner._update_states()` | `vllm/v1/worker/gpu_model_runner.py` |
| `prepare_inputs` | `GPUModelRunner._prepare_inputs()` | `vllm/v1/worker/gpu_model_runner.py` |
| `forward` | `GPUModelRunner._model_forward()` | `vllm/v1/worker/gpu_model_runner.py` |
| `compute_logits` | `Qwen2ForCausalLM.compute_logits()` | `vllm/model_executor/models/qwen2.py` |
| `sample` | `GPUModelRunner._sample()` | `vllm/v1/worker/gpu_model_runner.py` |
| `bookkeeping` | `GPUModelRunner._bookkeeping_sync()` | `vllm/v1/worker/gpu_model_runner.py` |

Each instrumented function keeps its original body in a corresponding
`*_inner()` method. The wrapper only calls `kperf_begin()` before the inner
method and `kperf_finish()` in `finally`.

`compute_logits` is instrumented only in `Qwen2ForCausalLM.compute_logits()`.
The runner retains its original direct `self.model.compute_logits()` calls.

## Baseline verification

Replace files only when the target is the matching vLLM 0.11.2 baseline:

```text
bb00863cbc7688e0e65c8a9f50eba9d2804ca4b75597ac342a622e49e46c2229  vllm/v1/worker/gpu_model_runner.py
207446413bac19e0cb44bacff10bad1dd0fb925b878d4b8f422ccd4e0c53baa7  vllm/model_executor/models/qwen2.py
```

If either target hash differs, do not overwrite it as a whole file.

## Replacement layout

Copy the three runtime files into the Python environment that starts vLLM:

```text
<site-packages>/kperf_instrument.py
<site-packages>/vllm/v1/worker/gpu_model_runner.py
<site-packages>/vllm/model_executor/models/qwen2.py
```

No Python extension rebuild is required. Restart the vLLM service after
replacement.

## Runtime environment

Set the PMU events explicitly for every collection run:

```bash
export VLLM_USE_V1=1
export KPERF_RAW_EVENTS='<comma-separated raw event codes>'
export KPERF_EVENT_NAMES='<comma-separated event labels>'
export KPERF_TIME_MODE=inner
```

`kperf_instrument.py` uses the Linux `perf_event_open(2)` interface directly;
it does not invoke the `perf` command-line program.
