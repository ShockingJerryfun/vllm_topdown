# InfraPerf

AI Infra 应用性能测试与关键数据指标采集脚本仓库。

## 目录结构

```
InfraPerf/
├── bench/      # 跑用例、产生 workload
├── collect/    # 采集原始数据（只采不算）
├── analyze/    # 把原始数据算成指标
├── common/     # 跨场景复用能力
├── tools/      # 一次性/辅助脚本
├── configs/    # bench/collect 参数配置
├── results/    # 运行产物（不入库）
└── docs/       # 约定与指标定义
```

## 分类原则

- **第一层按功能域**：bench / collect / analyze / common / tools
- **第二层按场景域**：infer（含 vlm、llm）/ train / system 等

定位脚本路径 = 「功能 → 场景」。例如 `collect/pmu/` 采集 PMU 原始事件，`analyze/pmu/` 把事件算成 IPC、CacheMissRate 等指标。

## 约定

- 采集与计算分离：`collect/` 只产出原始事件/日志，`analyze/` 只消费原始产物产出指标。
- 统一输出 schema：所有 `analyze/` 产物走 `common/schemas/` 定义的列名与单位。
- 配置与脚本分离：参数放 `configs/`，脚本读配置。
- `tools/` 放临时/辅助脚本，不要求稳定性。
- `results/` 入 `.gitignore`，数据产物不进版本库。

详见 [docs/conventions.md](docs/conventions.md) 与 [docs/metrics.md](docs/metrics.md)。

## 现有用例

- [vLLM Topdown 采集](docs/topdown/README.md)
