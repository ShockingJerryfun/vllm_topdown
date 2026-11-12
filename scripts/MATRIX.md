# 模型、代码页与 GPU 近远对照

`run_matrix.sh` 使用现有 `run_topdown.sh` 逐组采集，不修改频率。
每个模型做 4K/64K × near/far，各测一次。near/far 同时改变 Worker
CPU 和 Worker 内存节点，服务端及客户端核不变。

84 配置包含 Qwen3-8B-Instruct 和 GLM-4.7-Flash-AWQ-4bit，共 8 组。
GLM 配置实际声明 compressed-tensors；不额外强制 AWQ。
模型存在和分片完整不代表当前 vLLM、V2 runner、图路径及采集点兼容。
首组运行和验收才是兼容性证明。跨模型差异包含结构与量化差异，
页大小及近远的效果应分别在每个模型内部比较。

```bash
# 仅预览：不启动容器、不准备页、不启动模型。
bash /home/fj/topdown_binding/new/scripts/run_matrix.sh \
  /home/fj/topdown_binding/new/scripts/matrix_84.env

# 用户决定开始时使用；本次脚本交付未执行此命令。
bash /home/fj/topdown_binding/new/scripts/run_matrix.sh \
  /home/fj/topdown_binding/new/scripts/matrix_84.env --run
```

修改配置的 BATCH_ROOT 控制整批输出目录。目录必须是新目录，不使用时间戳。
例如 `Models_Pages_NUMA/Qwen3_8B/64k_near/r1/`。
每组保留现有三页 Excel（Topdown、Hotspot、SPE）和 details CSV；
批次 `index.csv` 指向各组实际工作簿，control 保存配置、日志和退出码。
脚本不改变工作簿格式和现有工作簿文件名。

默认继承 BASE_CONFIG 的 2 核、7000/100、单请求参数，强制 async OFF、
full、SPE 和频率观测。每组内复用服务和客户端；不同条件重启以应用
页副本、模型和绑核，独立 SPE 阶段沿用现有流程。

运行前 GPU 空闲且连续三次不高于 45°C，组内预热前冷却到 60°C，
现有监督器在 85°C 中止。GPU占用时等待并记录进程，启动前竞争占用会重试。
真实采集失败仍停止，不自动无限补跑。用同一命令加 `--resume` 接续已有批次，
配置必须与原批次一致，只跳过验收和清理均通过的组，不覆盖失败证据。
每组前要求至少 20GiB 可用，所需总容量取决于实测产出。

页准备使用 PAGE_IDENTITY 的代码/数据文件列表，从当前容器源文件建立
每模型每条件独立副本，64K prefault 时暂启 thp_exec_enabled，随后恢复。
代码大小和 NUMA 验收严格，驻留变化作为观测项；不要求每一页都是 64K。
依赖清单必须覆盖目标模型实际加载的文件。若 GLM 增加新库或 JIT 文件，
验收可能拒绝该组；须用该组真实映射补充清单后重新准备，不能降级为通过。

950 使用相同脚本，参考 matrix_950.env.example 填写该机路径、拓扑和
本机页清单；BASE_CONFIG 必须配置 CHIP=950、正确的容器、采集器、
Python、DevKit 等。页工具及 code_guard 必须在目标机编译和验证。
DevKit 若依赖工作目录，DEVKIT_BIN 指向先 cd 到安装目录再 exec devkit
的包装入口。不得直接套用 84 的核号、页副本或本机二进制。
950 未实测，不把可配置接口称作已完成移植验证。
