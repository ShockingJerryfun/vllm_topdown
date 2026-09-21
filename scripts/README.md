# Topdown 与 SPE 采集

统一入口 `scripts/run_topdown.sh [配置绝对路径 [结果目录]]` 完成函数阶段、端到端 time/PMU、hotspot 和 SPE，生成同一份 Excel。对明确设置 Worker 绑核的 920b/950 配置，`SPE_ENABLE=auto` 自动启用 SPE；显式 `SPE_ENABLE=0` 可关闭。其他芯片或未配置绑核时保留原 Topdown 流程，海光不运行 Arm SPE。

启用 SPE 时需在配置中提供 `HOST_PYTHON`、`SUBREAPER_BIN`、`EXPERIMENT_LOCK`、`SPE_BINARY_CACHE` 及 Worker/服务/客户端绑定。主入口复用现有监督器，在完整 Topdown 后独立采集 3 次正式请求的 SPE，使用原生快速解析；选中样本回放核对通过后删除该次临时全量 perf 数据，只保留筛选结果、必要二进制及校验证据。不会删除历史数据。Excel 输出 Topdown、Hotspot、SPE 三页，完整明细导出 CSV；Topdown 汇总样式保持。

`scripts/run_experiment.sh` 是单个条件的一键入口：time、13组PMU、独立SPE、原始记录校验、原样式Excel及资源清理。`scripts/sweep.py` 在已验证的页条件上安排核数与单因素对照。两个入口均在84宿主机执行，推理使用已检查的自有容器。

`HOST_PYTHON` 指向任务自己的宿主机虚拟环境，须具备 `openpyxl` 和 `regex`；容器内的报表依赖不能代替宿主机比较入口的依赖。84任务环境已按实际容器的 `regex` 版本验证，安装来源和校验值保存在任务证据目录。

## 单个条件

```sh
bash /home/fj/topdown_binding/new/scripts/run_experiment.sh \
  /home/fj/topdown_binding/configs/qualification_4k_4core.env \
  /home/fj/topdown_binding/results/example_4k_near
```

配置固定Worker核集合、四物理核候选池、服务/客户端核、NUMA节点、模型与请求。正式比较要求 `COLLECTION_PROFILE=end_to_end`、`SPE_ENABLE=1`、受控 `CODE_PAGE_CONDITION` 和实际 `CODE_PAGE_MODE`。示例结果目录须尚不存在；已存在的结果目录不覆盖。

Excel保留Topdown汇总样式，输出Topdown、Hotspot、SPE三页，明细存CSV。time、各组PMU与SPE分别测量。采样数不是指令执行次数，SPE时延字段不推导缓存供数层级。

## 分阶段比较

真实64KB近端和远端条件准备、验证完毕后：

```sh
/home/fj/topdown_binding/.venv/bin/python \
  /home/fj/topdown_binding/new/scripts/sweep.py \
  --settings /home/fj/topdown_binding/configs/matrix.json --phase all
```

先按交错顺序重复比较1/2/4物理核，确定核数后固定它，再重复比较64KB近端基线、4KB近端、旧脚本64KB近端、新脚本64KB远端。默认3次重复，共21个物理运行，不做全交叉。可用 `--phase cores` 或 `--phase comparisons` 单独执行阶段；后者必须已有验收通过的核数选择。`--prepare-only` 只检查先决条件、准备计划和配置，不启动采集。

核数选择同时看端到端时间、重复波动与TTFT/TPOT：改善未超过重复范围时不优先增加核数。这是描述性选择，不表示统计显著性或全局最优。新旧表示常驻切换与逐轮重启两条采集路径；两边都使用同一受控绑定、请求与验收口径。

只有完整质量、运行导入身份、线程绑定、实际页映射、SPE独立核对与清理验收通过的结果进入比较。完成的运行会复审后复用；失败目录保留，需核实原因后另行处理，不能伪造完成标记或清空原始证据。

## 页条件和证据

代码页实验控制独立文件副本的实际folio；系统基础页仍为4KB。64KB完整内部分组、文件边界、小库和已证明的运行时回调页分别统计，不能把全部代码标为64KB。页复制、冷却、预加载和验收见 [pages/README.md](pages/README.md)。未知匿名代码、页归属不明、前后代码页漂移均拒绝验收。

84上的64KB文件代码页准备涉及共享 `thp_exec_enabled`；本脚本不会自动修改共享开关。必须先解决该操作的授权和条件准备，不能用配置文字代替实际64KB证据。

原始运行保存在 `results/`，源与依赖身份、失败联调和辅助证据保存在 `evidence/`，页副本在 `conditions/`，共用二进制证据在 `binaries/`。比较工作簿输出到配置的 `output_dir`，保持一份当前权威结果。解码输入和逐条保留字段见 [spe/README.md](spe/README.md)。

全矩阵完成后，`output_dir/Topdown_对比.xlsx` 保留的基线结果页只来自一次运行：比较阶段的新脚本、选定核数、64KB近端基线第1次运行，不是三次平均，也不是所有条件合并。新增“逐次对比”“重复汇总”用于跨运行比较；“报告索引”明确标出唯一的原表来源，并列出各运行的条件、重复序号和比较阶段。

每个物理运行自己的结果报告按原字节复制到主工作簿旁的 `reports/<运行ID>.xlsx`，索引使用可移植的相对链接。同次基线用于多个比较阶段时只复制一次。请将主工作簿与整个 `reports/` 目录一起移动或交付。`comparison.json` 的 `report_bundle` 保存逐次文件SHA、相对路径和来源标记，路径基准为主工作簿所在目录；原始报告不修改。运行ID不安全、重名、同一运行身份冲突或已有副本内容不一致时停止发布。

独立 `compare.py --baseline-run ... --workbook-output ...` 使用同样的索引与打包规则，原表来源由显式参数指定，按传入清单汇总。带有 `sweep` 元数据的正式发布必须全矩阵完成；阶段未完成时仅保存工作区汇总，不发布最终工作簿。

### 汇总报告与 CSV 归档

各芯片入口使用 `--compact-report`：最终 Excel 保留 `Topdown`、`Hotspot`；
启用 SPE 时再加入 `SPE`。Topdown 保持原汇总布局，热点按函数表格展示。
`details/topdown/` 按原相对路径保存各组原始、解析、质量及汇总 CSV，
`details/index.json` 记录来源和 SHA256。原始文件仍保留。
`details/hotspot.csv` 保存完整热点列表；
`details/spe_samples.csv`、`details/spe_instructions.csv` 保存完整 SPE 明细。
SPE 页通过代码库、ELF PC 和运行时 PC 对照明细，完整 `pc_key` 仅留在 CSV 中，样本占比以筛选后总样本为分母；
事件占比以该指令含事件包的样本数为分母，不称为缓存 miss rate。
无事件包时留空；样本数不等于指令执行次数，延迟统计单位为 cycles。
未启用 SPE 时只有前两页。CSV 的地址、长标识需按文本导入。

### 独立推理频率轮

在支持 DevKit turbostat 的服务器上设置 `FREQUENCY_ENABLE=1` 和
`DEVKIT_BIN=/absolute/path/devkit`。主流程复用常驻服务与客户端，在预热后新增一轮
不启用 PMU/SPE 的正式推理，使用 `devkit tuner turbostat -d 300`，请求结束立即
以 SIGINT 收尾并读取工具的最终平均频率表；300 秒是异常保护上限，不是采集时长。
Topdown 的 Benchmark 下方仅显示 Worker CPU 集合的平均 Core MHz 与对应 NUMA
节点的平均 Uncore MHz。原始输出、每核频率 CSV 和采集时间保存在 `frequency/`。
频率窗口包围客户端请求，含短暂 IPC/检测开销，不是逐 Decode 精确打点。
Uncore 是 DevKit 的 NUMA Uncore 口径，不代表 DDR 频率；不支持时保持关闭。
