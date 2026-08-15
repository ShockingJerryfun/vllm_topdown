# vLLM performance source branches

This repository keeps complete upstream vLLM source baselines separately from
their kperf-instrumented variants.

| Branch | Contents |
| --- | --- |
| `vllm_0.11` | Unmodified upstream `v0.11.2` source |
| `vllm_0.11_perf` | `v0.11.2` source plus the agreed six-stage V1 probes and collection scripts |
| `vllm_0.26` | Unmodified upstream `v0.26.0` source |
| `vllm_0.26_perf` | `v0.26.0` source plus newly implemented six-stage probes for the default V2 runner and collection scripts |

Use the source branches as clean comparison baselines. Run measurements only
from the matching `_perf` branch and verify `vllm.__version__`,
`vllm.__file__`, the imported runner module path, and the Git commit before
collection.
