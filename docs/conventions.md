# 约定 (conventions)

本仓库脚本遵循以下约定，新增脚本请对齐。

## 1. 采集与计算分离

- `collect/` 只产出原始事件/日志，不做聚合计算。
- `analyze/` 只消费原始产物产出指标，不重新采集。
- `bench/` 只跑用例并落原始日志，不算指标。

缺数据时直接报错，不静默跳过、不就地补采。

## 2. 统一输出 schema

所有 `analyze/` 产物走 `common/schemas/` 声明的列名与单位：

- 列名小写下划线，如 `ttft_ms`、`ipc`、`cache_miss_rate`。
- 单位进列名（`_ms`、`_us`、`_pct`），数值为裸数字。
- 输出格式：JSON（结构化）+ CSV（表格化）二选一或并存，schema 中声明。

## 3. 配置与脚本分离

- 参数放 `configs/<功能>/<场景>/<用例>.yaml`，脚本读配置。
- 不硬编码模型路径、并发数、采集频率等。
- 便于换卡 / 换模型 / 换采样配置重跑。

## 4. run_id 与结果目录

- 每次运行生成 `run_id`（建议 `YYYYMMDD-HHMMSS` 或带场景标签）。
- 产物落 `results/<功能>/<场景>/<run_id>/`。
- `results/` 整体不入库（见 `.gitignore`），仅保留 `.gitkeep`。

## 5. 复用 common/

- 进程启停 / 后台采样 attach/detach 走 `common/runners/`。
- 原始格式解析走 `common/parsers/`。
- 不在 `bench/` `collect/` `analyze/` 内重复实现这些能力。

## 6. tools/ 是垃圾桶

- 临时绘图、环境探测、一次性汇总脚本放 `tools/`。
- 不要求稳定性，可随时增删；核心逻辑不得放此。

## 7. 命名

- 目录与文件名小写下划线。
- 脚本入口建议 `run_*.py`，分析入口建议 `compute_*.py`。
