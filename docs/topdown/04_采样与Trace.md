# SPE、热点与 Trace 分析指南

三种工具回答不同问题：CPU 热点说明采样事件集中在哪里；Arm SPE 提供被采中指令的访存、事件和计数器信息；CUDA Trace 连接 Host API、Graph Node 与 GPU 活动。阶段总量及 PMU 公式见 [PMU 与计时](03_PMU与计时.md)。

## 1. 选择采集方式

| 目的 | 方法 | 结果边界 |
| --- | --- | --- |
| 找 CPU 热函数和热指令 | `perf record/report` | 采样事件分布，不是函数调用次数 |
| 查看 CPU 访存样本、地址和采样延迟 | Arm SPE | 被采中的操作，不是全部访存或 GPU 内存延迟 |
| 对齐 CPU API 与 GPU Kernel | Nsight Systems | Trace 中真实发生的 API 和设备活动 |
| 解释 Graph 提交批次 | Graph 拓扑、驱动边界观测、对照程序 | 需要单独的提交边界证据，不能只数 API 或 Kernel |

各类采集分别运行。性能比较另用关闭 profiler 的干净请求，避免把采样、符号解析或 Trace 开销当作被测程序本身的耗时。

## 2. CPU 热点

### 2.1 配置与对象

在常规 `full` 配置中设置：

```bash
HOTSPOT_SCOPE=thread
PERF_EVENT=cycles:u
PERF_PERIOD=100000
```

`thread` 选择模型主执行线程，使用 `--no-inherit -t`；`worker` 选择 Worker 进程范围，使用 `-p`。应按问题选择范围，并保存实际 PID/TID 和启动时间。热点所用宿主 PID/TID 不一定等于容器内编号。

`PERF_PERIOD` 是事件周期，不是微秒、请求数或采样持续时间。减小周期会提高样本密度，也增加扰动。固定周期的 `cycles:u` 样本近似反映用户态周期分布，不覆盖该线程内核态活动。

常规运行命令见 [使用指南](01_使用指南.md)。扩展 `extra` 流程不自动采热点，不能仅凭配置出现 `HOTSPOT_SCOPE` 就认为已执行。

### 2.2 查看与解释

从采集记录确定热点文件路径，令 `HOTSPOT_DATA` 指向对应 `perf.data`：

```bash
perf report --stdio --no-children -i "$HOTSPOT_DATA"
perf annotate --stdio -i "$HOTSPOT_DATA" --symbol "$SYMBOL"
```

`--no-children` 查看当前符号自身的样本归属，避免把子调用累加视图误当自身开销。解析需与运行时一致的 ELF、Build ID、符号和映射。Python、JIT 或生成代码缺少对应符号支持时，可能只能定位到 native 栈、解释器或地址，不能据此认为业务函数没有执行。

一个符号占比高，首先表示本次所选事件和对象范围内样本多。要证明它造成性能差异，应同时检查绝对耗时、样本数、请求一致性，并进行保持业务语义的对照。

## 3. Arm SPE 采集

### 3.1 前置检查

要求处理器和内核支持 Arm SPE，perf 能识别 `arm_spe_0`。在目标 Linux 环境检查：

```bash
test -d /sys/bus/event_source/devices/arm_spe_0
cat /sys/bus/event_source/devices/arm_spe_0/type
perf version
perf list | rg -i 'arm_spe|spe'
```

还需具备采样与物理地址记录所需权限、足够 AUX 缓冲和输出空间。`pa_enable=1` 只是请求物理地址包，不能保证每个样本都含有效 PA。SPE 的 perf 事件语法见 [perf-arm-spe 手册](https://man7.org/linux/man-pages/man1/perf-arm-spe.1.html)。

### 3.2 通过监督流程运行

准备与 [使用指南](01_使用指南.md) 一致的任务配置，至少明确以下项：

```bash
COLLECTION_PROFILE=full
SPE_ENABLE=1
PLACEMENT_MODE=worker_set
WORKER_POOL_CPUS="<four_physical_cores>"
WORKER_CPUS="<selected_worker_cpus>"
WORKER_NUMA_NODE="<numa_node>"
SERVICE_CPUS="<service_cpus>"
CLIENT_CPUS="<client_cpus>"
NUM_PROMPTS=1
NUM_WARMUPS=0
HOST_PYTHON=/path/to/collector/.host_venv/bin/python
SUBREAPER_BIN=/path/to/tini
EXPERIMENT_LOCK=/path/to/locks/test.lock
SPE_BINARY_CACHE=/path/to/binary_cache
```

占位值替换后，在采集代码根目录执行：

```bash
bash scripts/run_experiment.sh "$CONFIG" "$RESULT"
```

监督器为 SPE 启动独立服务与采集流程，安装窗口标记，不与阶段 PMU 嵌套运行。该流程自行执行 **1 次预热和 3 次正式请求**。单请求固定输出 `N` 个 token 时，每次请求检查 `N−1` 个目标 `run_fullgraph` 窗口。

显式关闭 SPE 使用 `SPE_ENABLE=0`，不必减少 PMU 组或阶段。`RUN_EXTRA=1` 的扩展分支强制关闭 SPE；需要 SPE 时安排单独的常规监督流程。

### 3.3 实际 perf 参数

`scripts/spe/capture.py` 使用下列采集形式。`HOST_TID` 是映射并核对后的模型执行线程，`SPE_DATA` 是本次输出文件；该片段用于说明实际参数，完整窗口采集仍由监督流程负责：

```bash
perf record --no-inherit -t "$HOST_TID" --sample-cpu \
  -e 'arm_spe_0/load_filter=1,store_filter=1,jitter=1,ts_enable=1,pa_enable=1/u' \
  -c 1024 -m 64,4096 -o "$SPE_DATA"
```

| 参数 | 作用 |
| --- | --- |
| `load_filter=1,store_filter=1` | 选择 load/store 操作 |
| `jitter=1` | 对采样间隔增加抖动，减轻固定周期偏差 |
| `ts_enable=1` | 生成用于窗口筛选的时间戳 |
| `pa_enable=1` | 请求记录物理地址；仍需检查字段存在性 |
| `/u` | 用户态范围 |
| `--no-inherit -t` | 绑定已核对的执行线程，不自动覆盖其余线程 |
| `--sample-cpu` | 保留 CPU 信息，用于检查迁移和流归属 |
| `-c 1024` | 当前默认采样周期；不是每 1024 次内存访问必然生成一个完整样本 |
| `-m 64,4096` | 普通环形缓冲和 AUX 缓冲的页数；实际字节数乘系统页大小 |

采集器低层接口提供 `--period`，默认 1024；外层未传入该选项时使用默认值。不要自行添加一个未被读取的 `SPE_PERIOD` 配置字段并认为周期已改变。改变周期或缓冲后，检查保存的实际命令。

### 3.4 低层接口

需要单独安排 SPE、指定周期或使用已准备好的页面条件时，可直接调用捕获器。它自行启动和监督所需进程，不是附着到任意已有服务的工具。执行前确认资源独占，并提供能完整运行推理的可信配置。

```bash
"$HOST_PYTHON" scripts/spe/capture.py \
  --run "$SPE_RUN" --container "$CONTAINER" --reaper "$SUBREAPER_BIN" \
  --config "$CONFIG" --cpus "$WORKER_CPUS" \
  --binary-cache "$SPE_BINARY_CACHE" --python "$PYTHON_BIN" \
  --gpu "$GPU_ID" --period "$SPE_PERIOD_VALUE" --raw-retention all
```

| 选项 | 含义 |
| --- | --- |
| `--run` | 新的 SPE 结果目录 |
| `--config` | 推理、请求和放置配置 |
| `--cpus` | Worker 允许的 CPU 集合 |
| `--launch-cpus` / `--node` | 按配套放置方案指定启动 CPU／内存节点 |
| `--namespace-manifest` | 已准备的页面条件与 mount namespace 清单 |
| `--binary-cache` | 保留与采样匹配的二进制文件 |
| `--raw-retention all\|selected` | 原始数据保留策略；需要独立重解码时保留 `all` |
| `--max-temperature` | 异常温度终止阈值，默认 85℃；不是锁温 |
| `--skip-perf-dumps` | 跳过独立 perf 文本导出；这些交叉检查应标为未提供 |

正常停止必须等待 perf 刷新 AUX 数据，并保存退出状态。强制杀死采集进程后得到一个非空文件，不足以证明记录完整。

## 4. SPE 解码与验收

### 4.1 解码命令与产物

`SPE_RUN` 指向含 `spe_capture.json` 的目录。可以是监督流程的 SPE 子目录，也可以是低层接口的 `--run` 路径。

```bash
"$HOST_PYTHON" scripts/spe/decode.py --run-dir "$SPE_RUN"
"$HOST_PYTHON" scripts/spe/report.py \
  --workbook "$WORKBOOK" --spe-dir "$SPE_RUN/analysis"
```

解码默认输出到 `analysis/`；`--output` 可显式指定另一分析目录，`--native-library` 可选择配套 native 解码库。报表命令修改指定的工作簿，加入 SPE 分析页；不重新运行推理。

| 文件 | 用途 |
| --- | --- |
| `spe_capture.json` | perf 输入、CPU/TID、窗口、配置与关联元数据 |
| `data/perf.data` | 原始 perf/AUX 数据，用于独立重解码 |
| `analysis/samples.jsonl.gz` | 正式窗口内的标准化样本 |
| `analysis/pc_summary.json` | 按二进制身份和文件偏移聚合的 PC 统计 |
| `analysis/windows.json` | 预热／正式请求窗口及 Replay 范围 |
| `analysis/audit.json` | 流解析、筛选、排除原因与独立核查结果 |
| `analysis/manifest.json` | 处理状态、输入身份、产物和字段语义 |

解码开始会把 manifest 设为处理中，异常时写失败状态。不能只看目录里是否还存在一次成功输出。

### 4.2 时间窗口和样本范围

窗口标记与样本使用可对齐的 CNTVCT 时间基准，结合每个 AUX chunk 的 perf 时间转换信息。按照 `[start,end)` 筛选主执行线程、正式请求和 `run_fullgraph` 窗口；Replay 是其中的子范围。

一个 `perf.data` 可含多条 CPU/AUX 流。应逐流解析后按时间戳和 TID 归并，不能只取第一个流，也不能把文件中所有 SPE 包都算入正式结果。预热、窗口外、非目标线程、缺少必要字段的记录分别统计。

### 4.3 最低验收项

| 检查 | 通过条件 |
| --- | --- |
| 线程 | 容器与宿主 TID 对应，进程启动时间一致，实际执行线程符合配置 |
| 窗口 | 三次正式请求均完成，窗口数量符合 token 与执行路径预期 |
| 数据流 | AUXTRACE 类型、CPU/流信息、记录边界和时间转换有效 |
| 完整性 | 检查 lost、截断、冲突及解码错误；出现缺口不能默认为完整采样 |
| 聚合 | 所有 PC 的样本数之和等于选中样本总数 |
| 二进制 | maps、文件偏移、ELF 身份能对应，未知项明确保留 |
| 输出 | audit 与 manifest 都为通过，缺失的独立检查没有被写成通过 |

保留原始数据时，可从记录做独立检查：

```bash
perf script -D -i "$SPE_DATA" > "$PACKET_DUMP"
perf script -i "$SPE_DATA" --itrace=M \
  -F pid,tid,cpu,time,addr,data_src,weight,ip,phys_addr > "$MEMORY_DUMP"
```

两种视图的字段和生成事件可能不同，应按同一筛选窗口、操作类型和字段存在性比较。不要直接把 `perf report` 所有合成事件数当作唯一 SPE 样本数。

## 5. SPE 字段解释

| 字段 | 可说明的内容 | 不能直接推导的结论 |
| --- | --- | --- |
| PC／符号／指令 | 采中操作的执行位置 | 程序所有指令的执行次数 |
| samples／样本占比 | 当前过滤范围内的样本分布 | 指令总次数、耗时占比或 GPU 耗时 |
| operation／event bits | 原始操作类型和可同时出现的事件位 | 把各事件百分比当互斥分类相加 |
| counter 0：TOT | 样本总延迟计数 | 纯 DRAM 延迟 |
| counter 1：ISSUE | 样本 issue 延迟计数 | 与 TOT 相减后就是内存服务时间 |
| counter 2：XLAT | 地址转换相关延迟计数 | 所有 TLB miss 的完整时延分布 |
| counter 6 | 保留原始编号和值，语义未定 | 自行命名为某级 Cache 或总线延迟 |
| data source | 该处理器编码所提供的数据来源信息 | 通用缓存层级、GPU HBM 或精确 NUMA 源 |
| VA | 访问虚拟地址及其快照映射 | 访问瞬间的分配对象始终不变 |
| PA | 样本物理地址及可验证的资源归属 | 缓存命中位置、内存缓存策略或设备内部位置 |

延迟按存在该 counter 的样本求均值和分布，同时给出有效样本数。缺失值保留 null，不填零。SPE counter 的单位和含义按架构及实现解释，不能用 CNTFRQ 直接把所有 counter 转成纳秒。

data source 的覆盖率要单独报告，例如 `有 data source 的选中样本数 / 全部选中样本数`。缺少该字段不等于来源编码为零，也不等于 L1 命中。不同处理器、操作类型和实现对该字段的提供情况可以不同。使用 HIP12 对应解码时，`0x8` 表示 CPU 本地内存来源，不能把它重命名为 GPU HBM。

### 5.1 符号与地址归属

符号解析保留采集时的 maps、二进制身份及可执行段信息。跨运行比较采用“文件哈希＋文件偏移”，避免 ASLR 导致同一指令地址变化或不同指令地址碰巧相同。

PA 落入已保存的 GPU BAR 范围，可说明地址归属该资源；PA 落入 System RAM 且物理内存块能唯一对应 NUMA 节点，可说明 RAM 节点。它们都不证明某一级 Cache 命中、写合并策略、PCIe 事务次数或 GPU 内部显存访问。没有映射证据时保留未知。

## 6. CUDA Trace 采集与分析

### 6.1 收集 API 和设备活动

准备一个可独立运行、能完成目标推理并正常退出的驱动程序。`DRIVER_SCRIPT` 是应用自己的驱动，不是在线服务客户端；只跟踪 HTTP 客户端无法看到独立服务进程中的 GPU 工作。

在模型运行环境中执行：

```bash
nsys profile --trace=cuda,nvtx --sample=none --cpuctxsw=none \
  --cuda-graph-trace=node --output "$TRACE_PREFIX" \
  "$PYTHON_BIN" "$DRIVER_SCRIPT"
nsys export --type sqlite --output "$TRACE_SQLITE" "$TRACE_REPORT"
```

`TRACE_REPORT` 指生成的 `.nsys-rep`，`TRACE_SQLITE` 指导出的数据库。参数以已安装版本的 `nsys profile --help` 为准。`node` 展示 Graph 节点活动，开销可能明显高于整图视图；仅看整图时使用 `graph`。设置与限制见 [Nsight Systems User Guide](https://docs.nvidia.com/nsight-systems/UserGuide/index.html#cuda-graph-trace)。

为请求、Prefill、Capture、Replay 和目标语义范围设置清晰标记；只在需要的窗口跟踪。保存驱动版本、CUDA/vLLM/PyTorch、实际库路径和二进制身份，供后续 API 与源码对应。

### 6.2 按层建立对应关系

```text
模型语义算子
  → 具体实现与 launcher
  → Library / CUDA Runtime / CUDA Driver Host API
  → Graph Node 或直接 launch
  → GPU Kernel / memcpy / memset 活动
```

这些对象不是一一对应：一个语义算子可能调用多个 Kernel；一个融合 Kernel 可能覆盖多段语义；一个生成的 Triton launcher 也不等于固定融合算子。

分析时先确定请求与执行阶段，再使用 Trace 的 correlation、Graph/node 标识、线程与时间范围关联。GPU 活动时长来自设备事件；Host API 时长来自 CPU 侧调用。Host API 返回不代表 GPU 已完成。

Capture 中观察到的逐节点 Host 调用用于建图，不能称为 Replay 时逐算子重新下发。Replay 的 Host 边界通常是整图 launch 路径，语义算子的 GPU 活动需要通过图节点关联解释。

## 7. Graph 提交机制对照

`cudaGraphLaunch` 次数、Graph Node 数量、GPU Kernel 数量和驱动提交批次数是不同统计。若问题涉及驱动分批提交，需要在经过二进制身份核验的边界观察命令提交；本套分析以对应的 GP_PUT 更新与 doorbell 提交行为确定批次，不以 Kernel 数替代。

对照程序应一次改变一个结构因素：

| 对照 | 要固定的内容 | 检查内容 |
| --- | --- | --- |
| 节点数量／前缀扫描 | 节点类型、依赖与执行实现 | 批次变化是否对应特定结构边界 |
| 链深度／根节点数量 | 节点总数与工作量 | 拓扑与并行根的影响 |
| 普通依赖／PDL 依赖 | 节点、实际工作与输出 | 边类型、端口和依赖语义是否改变 |
| memcpy/memset／等价 Kernel | 数据范围、初值与结果 | 节点类型的影响及数值等价性 |
| 打开／关闭边界探针 | 同一执行与输入 | 探针对行为和耗时的扰动 |

导出拓扑时保留 node type、完整边集合、依赖类型及 from/to port。仅导出节点名字不足以复现 PDL 对照。替换节点后核对输出、同步和依赖；保持结果正确也不代表已证明性能提升。

专项探针依赖具体 Driver 二进制和地址映射，不能把一次运行的地址或偏移搬到另一构建。机制验证、数值正确性、无探针 CPU/GPU 时间和端到端性能分别记录。

## 8. 保留与复查

热点保留 `perf.data`、命令和匹配符号；SPE 保留原始记录、窗口、maps、ELF、解码审计和配置；CUDA Trace 保留 `.nsys-rep`、数据库、拓扑和驱动程序身份。

如果只保留聚合 CSV／JSON，就只能复核这些已保存字段，不能重新解释已删除的原始包或重新做独立解码。完整性、来源覆盖率和根因未知必须分别说明，不能用“报告已生成”代替。

代码核查点：`scripts/run_one.sh`、`scripts/spe/capture.py`、`requests.sh`、`decode.py`、`records.py`、`resolve.py`、`report.py`。Graph 专项同时检查实际使用的驱动程序、拓扑导出和探针实现。
