# common/

跨场景复用的能力，不含具体用例。

## 子目录

- `runners/` — 进程/子进程封装，attach/detach、后台采样启停、超时控制
- `parsers/` — 各类原始输出解析（perf、ncu、nsys、jsonl、csv）
- `schemas/` — 指标输出 schema（统一列名与单位）
- `utils/` — 通用工具（路径、时间、日志、run_id 生成）

## 约定

- 本目录代码被 `bench/` `collect/` `analyze/` 引用，自身不依赖它们。
- 不放具体场景逻辑，保持薄且稳定。
