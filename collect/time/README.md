# collect/time/

wall-clock 与阶段耗时打点采集。

## 形式

- 包裹被测命令记录起止时间
- 读取被测程序吐出的阶段耗时日志（如 vLLM 的 per-step metric）

原始时间戳/耗时落 `results/collect/time/<run_id>/`，TTFT/TPOT/throughput 计算在 `analyze/latency/`。
