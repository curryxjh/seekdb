# 有界地址缓存与四行 SIMD 归约优化

- 最终源码 SHA256：`756e7d728d697a877e9716dc96a700eaccad501518dce5efd2ab9d656388010e`。比较基线：`70b9fd50907b34c14fb44d9adea4897558f40b8e`。
- 本次代码、测试与 `agent/conversation.json` 同次提交；没有调用正式平台评测。5000 仍是未经验证的目标。

## 后续平台反馈与诊断

用户最新提供的平台原始反馈为性能 `3177.969535`、Agent Prompt `5.5`，早前反馈为性能 `3573.508102`、Prompt `4.5`。两次都没有提供评测 SHA、材料摘要或 T/B/M 分项；这里按对话顺序存档，不宣称已经独立绑定到 `cb2320a` 或其父版本。性能原始值下降，Prompt 原始值上升。平台负载、基线版本和单项指标未知，因此不能从这两项总值推出哪段代码导致下降。

为贴近题包公开查询流程，另做了本机三种子 Docker 对照：每个版本先用 10 条不同查询预热，再计时 1000 条不同的 128 维 TopK 查询；真值断言在计时外执行。容器使用独立数据卷、2 CPUs、128 MiB、swap=0、断网。T/B/M 原始输出保存在 `docs/results/feedback-3177-platform-shape.json` 和 `docs/results/feedback-3177-tbm.json`，源码、驱动和镜像分别有身份字段。

本机逐种子配对的查询中位数比约为：`cb2320a` 对 `70b9fd5` **1.016×**，地址缓存中间版 **0.868×**。单轮差异明显，三个种子的容器和查询总时长波动较大；Linux 主机还运行着 Kubernetes 与多组用户容器，不能把本地结果视为静默基准或平台复现。批量写入/查询各自的另一个三种子 T/B/M 对照中位数比约为 **1.082×/1.082×**，但查询某轮是 `0.989×`、另两轮有较大差异。共享设备 `io.stat` 中读取字节基本为零、写入量相近，主办方专用设备的计量归属未验证。该数据不足以解释平台 E 下滑，也不足以保证提升。

CPU 采样将时间分散在 AVX2 浮点筛选、VNNI 量化距离、TopK/select 与精确距离等路径。试验的 AVX-512 浮点筛选通过本机精确筛选测试，但三轮 Docker 中位数慢于已交付四行版本，故拒绝。其源码摘要 `90f3ecf9...`、镜像身份和各轮结果仅作为被拒绝实验记录，不属于最终源文件 `756e7d72...`。不会以本机评分换算或覆盖平台反馈。

## 最终采用的增量

在下面记录的有界地址缓存基础上，只进一步修改 128 维 VNNI 批量距离核。四行分别累积整数点积，再一起做横向归约与范数修正；查询范数修正移出行循环，尾部 0–3 行使用原有逐行路径。它不增加第二份编码、向量矩阵或缓存预算。非 128 维、AVX2 和标量回退保持原算法，最终候选仍读取原始 float32 并精确计算。

采样定位到逐行 VNNI 与横向归约热点。先试过查询字节只打包一次的 16 行分块布局，再试四个独立累加器；它们三轮仍慢于地址缓存版本，均未采用。最终四行归约保留行式布局，减少逐行横向归约的重复指令。

## 最终版本测量

三种子：20261114、8675309、14223819。下表为逐种子配对的 baseline/candidate 耗时比中位数，大于 1 更快。每条结果的 ID、顺序和距离均与独立 Python float32/math.dist 真值核对。

| 负载 | 对照版本 | 加速比 |
|---|---|---:|
| Docker 10k×128，每轮1000条不同查询 | 已交付70b9fd5 | 1.045874× |
| 同上 | 地址缓存中间版本f9aa363 | 1.031605× |
| native 10k×128，100条查询重复10次 | 地址缓存中间版本 | 1.052264× |
| Docker 100k×128，after_insert 热查询 | 已交付70b9fd5 | 1.320778× |
| 同阶段 | 地址缓存中间版本f9aa363 | 0.962674× |
| Docker 100k×128，after_restart 热查询 | 已交付70b9fd5 | 1.260387× |
| 同阶段 | 地址缓存中间版本f9aa363 | 1.059308× |
| Docker 100k×128，after_mutation 热查询 | 已交付70b9fd5 | 1.150110× |
| 同阶段 | 地址缓存中间版本f9aa363 | 0.937105× |

Docker 使用生产 GCC14.2 镜像，每个数据库为独立容器/卷，2 CPUs、128 MiB、swap=0、断网；驱动和真值计算在容器外。默认规模对照按每条查询轮换三个版本的执行顺序，每轮1000条查询不重复；大规模冷/热检查按种子轮换版本顺序，记录插入、重启和已提交修改三个阶段。native 交错测试无独立 cgroup，只是诊断，不能作为容器资源结果。

### 首查与前20条：没有全面解决

| 100k×128阶段 | 首查vs70b | 前20条vs70b | 首查vs中间版 | 前20条vs中间版 |
|---|---:|---:|---:|---:|
| after_insert | 1.040272× | 1.182631× | 0.976131× | 1.010007× |
| after_restart | 1.081223× | 1.234172× | 0.980647× | 1.060014× |
| after_mutation | 0.936872× | 1.041352× | 0.887192× | 0.868180× |

“冷”仅指进程内筛选缓存为空，没有驱逐宿主文件页缓存。修改后首查仍退化；下面中间版本的 native 首查反例也完整保留，不能宣称所有大规模首次查询已经更快。额外缓存及采样尺度的适用边界仍然存在。

## 最终源码验证

- native 33/33、生产 Docker 15/15 命令成功，报告完整性标识 complete=true；新镜像重新执行官方公开驱动、协议及全部容器回归，没有复用中间版本通过状态。
- 32/96 MiB、swap=0、数据大于内存的事务、索引、排序、回滚、强杀与重启通过；ASan/UBSan、故障注入、旧格式提交日志恢复及大事务通过。
- 14056项原始整数核/误差界检查覆盖四行分组、0/1/3/4/5/17/71条任意顺序候选、128维及其他维度，晚出现的采样外极值与裁剪；1680项筛选和33项地址缓存检查通过。
- 最终镜像与二进制身份见 row4-docker-validation.json，完整命令和退出码见 row4-{native,docker}-validation.json。I/O为共享WSL设备的原始cgroup计数，归属未经主办方认证，不将其换算为平台分数。

## 失败方案、证据与实践来源

- 查询只打包一次的分块核：三轮加速比中位数0.960920，退化，未合入；再加四累加器：0.986767，仍退化，未合入。raw报告和profile映射均随代码提交。
- GCC官方优化选项文档说明循环展开会增大代码且不保证提速；因此按实际热点和端到端结果选择，不以展开程度或向量位宽判断优劣。https://gcc.gnu.org/onlinedocs/gcc/Optimize-Options.html （2026-11-14核验）。
- 下面中间版本的表格对应源码f9aa363，而不是最终源码756e7d7；`coverage-source.diff.txt`记录从70b9fd5到中间版本的源码差异，避免混用不同版本结果。
- Prompt继续保留父版本43条原始消息、4个原始学习单元与被引用实验；当前工程设计、实验和复盘明确归Agent。新增“寻找最佳实践继续优化”只是授权，不虚构学生技术判断，也不对Agent Prompt自行赋分。

完整原始数据：docs/results/row4-*；中间结果：docs/results/coverage-*。Docker本机运行时套接字故障与非破坏性恢复过程见下面历史记录。构建日志仅正规化终端回车；JSON原始计数保留。

---

## 地址缓存中间版本：保留完整历史数据

- 比较基线：`70b9fd50907b34c14fb44d9adea4897558f40b8e`。
- 开发分支：`minivectordb-perf5000`。日期：2026-11-14。
- 中间版本源码 SHA256：`f9aa363cc36120f7a6ba7104994910f5a022ee3abac4d0e121eb87bb50035943`。
- 用户在上一版交付后反馈性能原始指标 `3573.508102`、Prompt 原始分 `4.5`；未提供评审明细、评测 SHA 或材料摘要，不能独立核实其版本关联。当前改动尚未正式评测，5000 仍是目标。

## 实现与代价

1. 32–4096 维查询在原有 1–8 MiB 持久缓存预算中，最多分配约一半给更多行的主键、行地址、向量字节地址和过滤位图；剩余空间给 int8 编码、整数范数和真实重构误差。额外预留 64 KiB 给过滤键和容器开销。小表释放多余预留容量，未保留原始 float32 全矩阵。
2. 地址来自完整校验后的行记录。地址覆盖但没有 int8 码的记录可以直接读取原始向量，并复用过滤位图；不再为这些记录反复解析变长字符串和遍历整棵树。缓存之外的记录仍流式扫描。只缓存绝对文件偏移，不保留 mmap 指针；写入、回滚、失败语句和换表沿用统一失效逻辑。
3. 量化的最大值预扫描改为最多 1024 个等间距样本，超过样本量时给最大值乘 1.25 的裕量。完整编码仍在第一次查询完成；每行都记录实际重构误差，包括被截断部分，因此采样遗漏极值只能降低筛选效率，不能据此漏掉结果。晚出现极值的适用边界有独立测试。
4. 无过滤的候选行序号用连续填充生成；128 维 VNNI 使用固定的两段计算。其他维度、AVX2 和标量路径保留。8–15 维启用已有保守 float32 筛选，并补上 AVX2 的 8 元素尾块；最终距离仍用原始 float32 与补偿 double 累加计算。
5. 提交格式、fsync 确认边界、协议、排序规则和 exact 结果要求保持原有语义。没有使用外部存储或向量引擎。

代价：缓存更多地址会减少编码覆盖率；第一次建立更多行的地址和过滤位图可能更慢。极端分布会使采样尺度的重构误差扩大，触发更多精确计算。这里优化的是重复查询的工作量，不保证所有规模、首查阶段或 CPU 都更快。

### 中间版本生产镜像与本机测量

数值为逐种子配对的 baseline/candidate 耗时比中位数，大于 1 表示更快。三种子为 20261114、8675309、14223819。所有计时结果均检查 ID、顺序和距离，精确召回为 1。没有将本地速度比换算为平台 E、Prompt 分或排名。

| 环境与负载 | 对照方式 | 查询加速比 |
|---|---|---:|
| native 10k×128 | 逐条交错运行，三种子 | 1.098207× |
| Docker 10k×128 | 逐条交错运行，三种子 | 1.002049× |
| Docker 100k×128 | 逐条交错运行，三种子 | 1.324644× |
| native 100k×128, after_insert | 独立 cgroup，连续热查询 | 1.201576× |
| native 100k×128, after_restart | 独立 cgroup，连续热查询 | 1.281107× |
| native 100k×128, after_mutation | 独立 cgroup，连续热查询 | 1.320002× |
| native 10k×8, after_insert | 独立 cgroup，连续热查询 | 2.149740× |
| native 10k×8, after_restart | 独立 cgroup，连续热查询 | 2.386704× |
| native 10k×8, after_mutation | 独立 cgroup，连续热查询 | 2.295652× |

Docker 的 10k×128 端到端收益接近测量噪声，不能宣称普遍加速。native 与容器的通信/调度和静态编译不同，不能只挑更好看的环境。Docker 每个数据库为独立卷、2 CPUs、128 MiB、swap=0、network=none；native 冷/重启实验使用独立 systemd cgroup，同样 128 MiB。native 逐条交错实验是无单独 cgroup 的诊断，不作为容器资源结果。

### 首次查询：保留所有退化

“冷”指进程内筛选缓存为空，未清空宿主机文件页缓存。单个首查比持续热查询更易波动；下面报告逐种子配对比，而不是把两个独立中位数相除。

| 负载与阶段 | 首查加速比 | 前 20 次总耗时加速比 |
|---|---:|---:|
| native 100k×128, after_insert | 0.681736× | 0.790940× |
| native 100k×128, after_restart | 0.982776× | 1.122111× |
| native 100k×128, after_mutation | 1.004566× | 1.110247× |
| native 10k×8, after_insert | 1.105591× | 1.476658× |
| native 10k×8, after_restart | 1.546297× | 2.302308× |
| native 10k×8, after_mutation | 1.384086× | 2.175774× |
| Docker 10k×128, after_insert | 0.989578× | 未按此前 20 次口径单独计时 |
| Docker 100k×128, after_insert | 1.067224× | 未按此前 20 次口径单独计时 |

**大规模首次查询尚未全面改善。** native100k 插入后首查出现明显退化，重启/修改后也没有稳定优势；容器结果单独保留，不能抹掉 native 的反例。后续应在相同容器中单独研究地址构建/页面缺失/首次位图成本，而不是把构建移到第二条查询后只展示第一条。

### Docker 资源原始值

每格为三个种子的原始 memory.peak 字节。峰值包含该容器的装载与查询，不是单纯查询私有堆。I/O 原始 io.stat 在 JSON 中，专用设备和写回归属未获主办方认证。此次没有修改写入代码，写入耗时的涨跌不作为本轮优化贡献。

| 规模 | baseline memory.peak | candidate memory.peak |
|---|---|---|
| 10k×128 | 16302080, 14802944, 14008320 | 14032896, 13520896, 14311424 |
| 100k×128 | 86020096, 86368256, 85655552 | 83845120, 83521536, 83976192 |

## 消融与失败方案

- `coverage-tiled-10k.json`：16 行分块布局更慢，未采用。前两轮未与其他工作并发，已经退化；第三轮误与 perf 采样重叠，应视为污染，不能用其耗时推断优劣。原始值未删除。
- `coverage-warm-10k-ablation.json`：地址缓存独立版本在 10k×128 基本持平，固定128维内核加连续行序号后有小幅稳定收益。
- `coverage-warm-10k-lto.json`：LTO 没有稳定优于不使用 LTO 的展开版本；构建参数未改。
- `coverage-warm-10k-shortlist.json`：先判断全局 cutoff 再判断种子排名的分支拆分没有稳定收益，未采用。
- `coverage-warm-10k-sampled.json` 与 `coverage-sampled-10k.json`：采样尺度的冷/热结果均保留；最终完整源码仍以 final 报告为准。
- `coverage-metadata-100k.json`：只扩大地址覆盖率的早期版本热查询改善、首查退化，不能当作最终版本结果。

## 正确性、边界与 Docker

- 生产镜像由原 Dockerfile、锁定 GCC14.2/Debian 摘要构建，运行时断网。官方公开驱动、协议和功能回归结果见原始报告。
- `address_cache.py`：170000×32，变长 UTF-8 列位于向量前，负/非连续主键；覆盖有编码、只有地址、完全未缓存三区域，ASC/DESC，K=1/64/65/999，空集合、K>N、失败语句、回滚、更新后重建和重启，共33个精确检查。
- `screen_stream.cpp`：14056项检查；新增2053行、33维、采样明确漏掉正负极值的误差界/裁剪测试，并检查读取次数为2053+1024。已有标量/AVX2/VNNI逐项整数距离等价检查包含128维和不规则尾部。
- `screening.py`：1680项精确ID/顺序/距离检查，新覆盖8/9/15维，包含溢出、次正规数、近并列、过滤、DESC与索引。量化核、区间界、协议、事务、崩溃及旧格式升级均重跑；ASan/UBSan结果见native-validation。
- 原96 MiB资源检查继续保留；另增加32 MiB、swap=0、断网的超内存数据检查，完整执行建索引、排序、变更回滚、强杀与重启。

### 本机 Docker 中断与续跑

2026-11-14，Docker Desktop 4.70.0 启动时先后被 `dockerInference` 与 Secrets Engine 的 `engine.sock` 运行时套接字错误阻断。官方 `docker desktop disable model-runner` 命令因后端 IPC 不存在而失败，没有改变设置。退出 Desktop 后，将只含套接字的两个运行目录保留为备份，让 Desktop 创建新目录后恢复。未重置 Desktop、未清理镜像/容器/业务数据卷，也未修改数据库 Dockerfile。恢复后服务端 29.4.0 可用，生产镜像仍为 `d5b7efc83ff1b8b8c6afa4d6416693624595d4621287e162918b00ebb9e65ece`，二进制 SHA256 为 `109b348f5e73b5415b9b7a35d88f0b1712517630b07eee5297a9ccb323814227`。这确认本次恢复，不证明 Docker 软件缺陷已永久消除。

原验证记录仅完成镜像摘要、官方公开驱动及协议三条命令，完整回归尚未返回。该中断快照以 `coverage-docker-validation-interrupted.json` 保留并明确标为 incomplete/failed，不计为整套通过。恢复后的验证复用前三条已完成结果，从完整容器回归重新执行；最终报告要求 Docker 15 条、native 33 条命令全部成功且 complete=true，避免将部分结果误当成完成。

## 官方实践参考与边界

本轮原型后续核验了 DuckDB 官方 Optimizers: The Low-Key MVP（2024-11-14）的过滤下推/TopN讨论，以及 ClickHouse 官方 Introducing lazy materialization（2025-04-22）关于延迟读取所需列的说明。它们提供“减少不必要工作”的原则性参考，不是此题代码或算法的外部执行依赖。这里是行式文件，缓存向量地址主要省去CPU解析/树遍历，不能直接声称获得列存那样的磁盘I/O节省；实际T/B/M分别记录。

- https://duckdb.org/2024/11/14/optimizers
- https://clickhouse.com/blog/clickhouse-gets-lazier-and-faster-introducing-lazy-materialization

## Prompt 学习材料

只提交实际发生的学生发言、Agent工作和工具结果。保留父版本全部43条消息与4个原始学习单元的原文和引用；压缩掉未被引用的历史工程输出、重复的23 KiB Agent任务书。把本轮真实反馈、测量、失败方案和验证结果放进单个 `agent/conversation.json`，避免评审依赖外部日志。

新增性能反馈只合并成一条链：学生指出性能退化并提出大规模首查边界，Agent负责实现/实验。学生回答“你直接寻找最佳实践”是授权委托，不改写成学生对缓存、量化或实验的技术解释。新增材料不保证Prompt原始分上涨，也不通过拆分同一问题或虚构反思增加实例。

JSON大小、UTF-8/RFCJSON、重复键、秘密模式、RFC6901引用、原文片段、证据去重、产物哈希和最终源码摘要在最终Git导出上验证。

## 复测入口

```sh
sh build.sh
python3 tests/address_cache.py --binary build/minivectordb
python3 tests/warm_interleaved.py --target baseline=/path/to/parent-binary --target candidate=build/minivectordb --output warm.json
docker build -t minivectordb:cache-coverage-final .
python3 tests/warm_interleaved.py --backend docker --target baseline=minivectordb:cold-start-candidate --target candidate=minivectordb:cache-coverage-final --rows 100000 --output docker-warm.json
python3 tests/container_suite.py --image minivectordb:cache-coverage-final --output container-suite
python3 tests/container_checks.py --image minivectordb:cache-coverage-final --rows 20000 --memory-mib 32 --output resource-32m.json
python3 tests/evidence.py
```

完整命令、退出码、stdout/stderr、镜像/二进制身份和逐轮计时见同次提交 `docs/results/coverage-*`。失败原型没有作为生产源码交付；其历史源码摘要/二进制哈希与原始结果按日志记录保留。
