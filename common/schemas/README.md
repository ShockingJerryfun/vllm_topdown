# common/schemas/

指标输出 schema 定义（列名、单位、类型）。

所有 `analyze/` 产物应在此声明其 schema，便于跨场景汇总与下游绘图。
建议用 pydantic 或 dataclass 描述。
