# bench/

跑用例、产生 workload 的脚本。

## 子目录

- `infer/` — 推理场景
  - `vlm/` — VLM 推理 bench（如 vLLM/SGLang 上跑 Qwen2-VL）
  - `llm/` — LLM 推理 bench
- `train/` — 训练场景

## 约定

- bench 脚本只负责"跑用例 + 落原始日志到 `results/`"，不做指标计算。
- 参数从 `configs/` 读取，不硬编码。
- 输出路径统一通过 `common/runners/` 封装传入。
