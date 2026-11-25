# bench/infer/vlm/

VLM 推理 bench 用例脚本。

## 约定

- 输入数据集路径、模型路径、并发数等从 `configs/` 读取。
- 原始请求日志（per-request 时延、首 token 时延）落 `results/bench/infer/vlm/<run_id>/`。
- 指标计算交给 `analyze/latency/`，本目录不算指标。
