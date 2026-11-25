# configs/

bench / collect 的参数配置（yaml / toml / json）。

建议按 `<功能>/<场景>/<用例>.yaml` 组织，例如 `bench/infer/vlm/qwen2-vl-vllm.yaml`。
脚本读配置而非硬编码参数，便于换卡/换模型重跑。
