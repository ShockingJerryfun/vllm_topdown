# bench/infer/

推理场景 bench 用例。

## 子目录

- `vlm/` — 视觉语言模型推理 bench
- `llm/` — 纯文本 LLM 推理 bench

## 典型用例

- vLLM / SGLang / TensorRT-LLM 上跑 Qwen2-VL、InternVL 等
- 在线服务：TTFT/TPOT/throughput
- 离线批量：tokens/s、总耗时
