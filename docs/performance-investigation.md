# MiniVectorDB 性能调查与下一轮实验设计

日期：2026-11-16。对象：`minivectordb-perf5000`，调查起点
`b1a3cc30e7866fb7837599eff22b25f9d3a518c1`。

**结论：当前没有证据证明起始候选的小堆是主要瓶颈，也没有稳定证据证明上一轮数字解析修改改善了端到端性能。下一步应先测阶段占比，再独立比较候选选择算法。本报告不包含生产代码修改、新的性能测量或正式平台提测。**

旧对照为 `5572448a2602683ded0813cfca137cf98af5f418`，新对照为
`6b4b0d2b76f13210f75968638916e6672a61635d`；二者生产差异仅在
`sql.cpp` 的向量数字扫描。调查起点与新对照的生产实现、Dockerfile、
构建脚本相同。当前 `minivectordb/*` 摘要为
`a2553e08d54269ddea41679914b7d5d03f30369ab07178f87be21f3dd66ad6d3`。

本文区分三类信息：**代码事实**来自当前实现；**已有实测**来自注明身份的保存报告；
**预测/计划**尚未执行。所有工程判断由 Agent 提出，不作为用户已经独立发表的判断。
用户关于下一轮候选选择的预测、反证条件、实验后解释均仍待回答。

## 第一部分：真实代码执行链路

### 1.1 阅读范围、符号与入口

已通读独立数据库的 `main`、协议、SQL、数据库、存储、量化筛选实现及头文件，
核对构建脚本，并检查下文列出的基准、资源和相关正确性脚本。范围是
`minivectordb/` 实现及其相关测试，不是整个历史 OceanBase 代码库。

记表行数为 N、向量维度为 D、过滤后有效行数为 m、LIMIT 为 K；
缓存地址行数为 A，其中有 int8 编码的前缀为 C；真正调用补偿求和精算的行数为 R。
U 是起始种子堆的替换次数，V 是最终结果堆的替换次数。**m、U、V、R 含义不同。**

| 环节 | 当前函数位置 | 代码事实与相关成本 |
|---|---|---|
| 启动恢复 | `main.cpp:45` → `Database::Database`，`database.cpp:391` → `Storage::load`，`storage.cpp:201` | 恢复目录、提交根和有效文件尾，再输出 ready；不是先把所有向量载入内存 |
| 请求接收 | `main.cpp:21`，`read_line` | 64 KiB `read` 缓冲，`memchr` 找 JSONL 换行；请求上限 16 MiB；O(请求字节数) |
| JSON 解析 | `protocol.cpp:10`，`JsonParser` / `parse_request:180` | UTF-8 校验、字段和重复 key 检查、SQL 字符串解码；普通字符串快速路径仍有复制 |
| SQL 解析 | `sql.cpp:156`，`Parser`；`parse:419` | 再检查 SQL 的 UTF-8；token 和 literal 持有字符串；O(SQL 字节数)，有分配/复制 |
| 执行与响应 | `main.cpp:77` → `Database::execute:475` → `Spool::output:363` | 每请求构造响应对象，完成结果后才输出；每条响应 flush；不能为提速改变协议或延迟必要响应 |

入口核查：[main.cpp](../minivectordb/main.cpp)、
[protocol.cpp](../minivectordb/protocol.cpp)、
[sql.cpp](../minivectordb/sql.cpp)。
下列行号对应调查起点，后续源码变化后应重新定位。

### 1.2 写入：解析、记录、树与提交

实际 INSERT 链路：

```text
JSON SQL 字符串 → Parser::statement → Database::execute
  → 保存 Catalog/语句文件边界，invalidate_scan
  → 列映射/类型检查
  → convert → vector_literal → vector_number → decimal → double → float32
  → Row(vector<variant<...>>)
  → Storage::find 检查重复主键
  → put_row → encode → Storage::blob
  → Storage::put → insert/node/save_node
  → 若有向量索引，再追加完整向量 posting 和另一棵 B+ 树
  → 自动提交：commit → publish → flush/checksum/footer/fsync
  → 完整成功响应
```

| 阶段 | 位置 | 数据结构、复杂度及开销 |
|---|---|---|
| 数字转换 | `sql.cpp:119,390`；`database.cpp:405` | 常见路径 `from_chars<double>` 后转 float32；失败路径保留 `strtod` 的下溢舍入；O(向量文本字节数 + D)。直接改成 `from_chars<float>` 会改变两次舍入的语义 |
| 行组织 | `database.cpp:531` | 每行 `Row`、向量和部分字符串分配；列映射每语句构建；保存点复制 Catalog，但不复制全表数据 |
| 序列化 | `database.cpp:432` → `storage.cpp:368` | float32 位模式写入 string；`blob` 再构造“长度 + 内容”string 并复制，调用 `pwrite`；O(行字节数) |
| 主键树 | `storage.cpp:412,434,446,487` | 4096 字节页、每节点最多 255 项、512 页 LRU；按语句边界 COW，新建页可在同语句内复用。查找的逻辑比较 O(log N)，但节点按值返回，实际还包含节点 entry vector 的复制 |
| 节点更新 | `Storage::insert/save_node/cache_node` | 路径上的 Node 复制、entry 插入/移动、LRU 和哈希表维护；最坏每层 O(B)，B≤255，故不能只按比较次数估算写入 CPU |
| 向量索引 | `database.cpp:441,515` | 自有 posting，额外存完整向量并维护第二棵树；本次基准 `indexed=False`，这项不在该基准写入路径内 |
| 提交 | `storage.cpp:299` | flush 脏树页 → 截定长度 → 序列化 Catalog → `checksum_region` 读回本次新增区间并校验 → 追加 Catalog/footer → data `fsync` → 确认 |
| 失败与回滚 | `database.cpp:488,582`；`storage.cpp:348` | 恢复 Catalog 保存点并 rewind；失效查询缓存；显式事务已成功语句仍可提交/回滚 |

当前已采用**数据文件内嵌提交记录、常规提交一次 data fsync**，不是旧版每次
重命名目录文件的多次同步方案。初次兼容 guard 另有同步；大事务每新增约 8 MiB
提前同步以控制脏页，但不发布未提交根。不能省略确认前的持久化来获得写入收益。

10000 行、每批 100 行意味着计时写入含 100 次自动提交。解析、复制、树维护、
校验读回和 fsync 的实际占比均尚未针对当前二进制测出。
`pread` 命中页缓存也要做 CPU 拷贝/校验，不等于必有等量物理磁盘读取。

恢复的 `latest_catalog`（`storage.cpp:157`）按最多 1 MiB 块向后找有效 footer，
验证 Catalog 及对应提交数据。正常恢复不是每次重新解析所有历史 SQL；
破损尾部会增加回溯工作。`Storage::bytes:137` 按需 mmap，文件增长可触发重新映射，
因此跨写入保留裸映射指针不安全。

### 1.3 查询：两个堆，三种缓存区域，两层安全筛选

当前 K=10、D=128 的主路径：

```text
SELECT 绑定投影 / ORDER BY / WHERE
  → 必要时懒建地址前缀、int8 编码、重构误差
  → WHERE 位图命中或构建；先筛出有效行
  → query int8 编码、query norm/error
  → 批量整数码距离
  → 第一个堆：选 K 个量化起始种子
  → 按当前顺序复核种子，建立第二个堆：精确结果堆
  → 用精确阈值 + 重构误差计算安全 int8 cutoff
  → 其余每个有效编码行均做 cutoff 检查
  → 未拒绝者：float32 保守筛选 → 必要时原始 float32 的 double 补偿精算
  → 地址缓存中未编码行、缓存外尾部也不能遗漏
  → 最终结果堆排序 → 只取获胜行投影 → JSON/Spool/flush
```

主路径位置：[database.cpp](../minivectordb/database.cpp)，
`Database::select:588`；筛选实现：[screen.cpp](../minivectordb/screen.cpp)。

| 阶段 | 函数/位置 | 成本与实际行为 |
|---|---|---|
| 绑定 | `select:589`、`BoundExpr:197` | 解析查询向量 O(D)；ORDER BY 投影别名会复制 BoundExpr/query；相同距离投影会复用已算的 order key |
| 执行策略 | `select:653` | 有序且 K≤16384、距离或 BIGINT 排序用最终堆；其余可能走 4 MiB run 的外排序。主键等值走 `Storage::find:434`，不必全表扫描 |
| 地址缓存 | `select:718`；`database.h:47` | 单表 `ScanRow{id,offset}` AoS + 单独 vector byte offset 数组；每次修改前失效；回滚 offset 可复用，不能仅凭 offset 猜缓存仍有效 |
| 量化缓存 | `select:749` → `QuantizedScreen::build:370` | D 在 32..4096 时，int8 row-major codes、int32 norms、double errors；最多 1024 行抽样定 scale，然后编码全部 C 行，O((C+min(C,1024))D) |
| 三个区域 | `select:754,859,866` | int8 编码前缀；仅有地址但未编码的前缀；缓存外树尾部。尾部仍扫描并过滤，不能将 C 当成整个 N |
| WHERE | `select:768`、`RowView::matches:95` | 4 槽 packed bitmap；miss 解析 A 行字段并匹配，hit 枚举 set bit；长于 4096 字节的 key 不保留。已先过滤再算码距离/原始距离 |
| query 量化 | `screen.cpp:396` | 每查询 O(D)，分配 query codes，norm/error 各计算一次 |
| int8 距离 | `screen.cpp:409,425` | 有效编码行 m_c 的 O(m_c D) 工作；已用 AVX2/VNNI 等分派，输出 int32 distances。无过滤 AVX2 已是连续路径；无过滤 VNNI 仍构造 iota rows |
| 起始堆 | `database.cpp:843` | `Seed=(有符号码距离,扫描序号)`；O(m_c + U log K)，最坏 O(m_c log K)，空间 O(K) |
| 初始精算 | `database.cpp:850` | 起始种子按堆顶由差到好取出；再经 `accept_vector`；种子顺序影响过程中阈值，比较新选择器时必须固定顺序 |
| 全局安全界 | `screen.cpp:471`；`database.cpp:853` | 用种子得到的真实 worst + 全编码前缀最大重构误差；O(1) 算 cutoff，O(m_c) 检查余行；该第二遍的 int8 cutoff 当前固定，不随后来精确堆变好而更新 |
| float32 筛选 | `database.cpp:677`、`screening_square:141` / `screening_avx2:160` | 精确堆满后，对进入者算全 D 维粗平方距；有误差膨胀、subnormal slack、nextafter 阈值；非有限值回退 |
| 最终距离 | `refined_distance:121` | 原始 float32 转 double，差平方、补偿累加、sqrt，再用 fma 修正根；O(RD)。**不是普通 float SIMD 距离，也不是量化距离** |
| 最终堆 | `accept:669`、`Sorter::better:220` | Candidate 含 variant key/id/offset；最坏 O(R log K)，实际是检查 + V 次替换；相同返回距离时主键升序，ASC/DESC 均如此 |
| 输出 | `emit:638`、`Spool::write:320` | K 行排序 O(K log K)，只对获胜行解析普通列；每次 emit 新建 RowView fields；完整 VECTOR 投影才完整 decode。Spool≤4096 字节内存，超限落临时文件 |

**复杂度归纳：**暖查询约为
O(SQL字节数 + D + 位图枚举 + m_c D + m_c + U log K + R D + V log K + K log K)，
另加未编码/缓存外区域的扫描和精算。冷查询额外包含地址扫描、量化构建、首次谓词位图。
公式不能替代实际阶段时间；R 可能远小于 m，也可能接近 m。

### 1.4 当前规模下可以推导、不能冒充测量的数量

128 MiB 下 `scan_budget`（`database.cpp:21`）为 8 MiB。按代码预算，当前
10000×128D 全部可编码；保留内容大致为 1.28 MB codes、40 KB norms、
80 KB errors、240 KB 地址元数据，以及最多 5024 字节的四个位图，
合计约 1.57 MiB，不含 allocator、容器对象/谓词字符串及首次构建临时空间。
这不是进程/cgroup 峰值，也不是缓存一定只占此数值的承诺。

无过滤每查询 m=10000；25% 过滤时 m=2500。1000 条混合查询有
500×10000 + 500×2500 = **625 万次有效行机会**，约 8 亿个 int8 维度点积项。
这是工作负载与代码推导，**不是 625 万次精算、不是指令数、不是磁盘 I/O 字节数**。
基准的过滤谓词实际上只有 `g=1` 和 `g=3` 两种，位图热命中占优势，
不能据此推断任意高变化谓词的性能。

### 1.5 已有精度保护及安全界的适用范围

令原始向量 x、查询 q，重构向量为 s·c_x、s·c_q，保守重构误差为 e_x、e_q。
实数三角不等式给出：

```text
d_code = s · sqrt(||c_x-c_q||²)
max(0, d_code-e_x-e_q) ≤ ||x-q|| ≤ d_code+e_x+e_q
```

码距为 `norm_x+norm_q−2dot`。最大为 4096×254²=264257536，
在当前编码/维度范围内不溢出 int32；stored norm 已在构建时计算，
query norm 每查询一次，**没有逐候选反复计算原始向量范数的代码路径**。

实现还对量化重构、累加、开方及阈值加入误差膨胀和舍入余量
（`screen.cpp:18,55,458,471`）。实数公式本身不是对所有浮点行为的完整证明：
任何新剪枝都必须证明它仍覆盖当前 `refined_distance` 的返回值和并列比较，
不能仅给实数推导后删掉现有 slack。
ASC 仅在保守下界严格大于可接受阈值时拒绝；DESC 要用保守上界严格小于阈值。
边界相等须保留，避免漏掉距离并列但主键更小的行。

更紧且理论安全的方向包括：逐行 e_x 代替全局最大值；在精确堆变化时更新界；
对互不重叠维度块建立平方下界并相加；多个独立保守区间取交集。
它们都增加检查/元数据成本，数学更紧不代表运行更快。
此前逐行误差原型收益不稳定，见第三部分。

部分精算提前终止在 ASC 下有数学机会：剩余非负项不会减小实数平方距离。
但直接用“当前普通浮点部分和 > worst²”不是完整的工程证明：
累加修正、乘法/减法舍入、开方修正与距离并列都要有保守区间。
应使用向外舍入的下界和当前阈值上界；不明确就走现有完整精算。
DESC 仅有部分和下界通常无法提前排除，还需剩余部分的安全上界。
不能用改变求和顺序、`fast-math`、范数恒等式消去误差保护来获得速度。

## 第二部分：当前已知问题

### 2.1 已证实

已有同条件记录位于
[baseline.json](results/nov16-prediction-baseline/baseline.json)、
[candidate.json](results/nov16-prediction-baseline/candidate.json)、
[comparison.json](results/nov16-prediction-baseline/comparison.json)、
[identity.json](results/nov16-prediction-baseline/identity.json)。

条件：2 CPU、128 MiB、swap=0、10000×128D、每批100写入、1000 条不同 Top10、
一半25%过滤；Apple Silicon 模拟 Linux amd64；10 条预热；三个种子，
相邻运行，中间轮交换版本顺序。

| 中位数，原始测量的描述统计 | 旧 5572448 | 新 6b4b0d2 |
|---|---:|---:|
| 写入耗时 ms | 1654.681 | 1663.052 |
| 写入原始 I/O MiB | 6.531 | 6.531 |
| 写入 cgroup 峰值 MiB | 19.953 | 22.273 |
| 查询耗时 ms | 485.426 | 490.782 |
| 查询项累计原始 I/O MiB | 6.531 | 6.531 |
| 查询项累计 cgroup 峰值 MiB | 23.312 | 23.504 |

查询三轮旧/新为 441.529/537.190、600.890/485.086、
485.426/490.782 ms。逐对旧/新耗时比分别约 0.822、1.239、0.989，
中位数 0.989087；方向反转，**稳定提速证据不足**。
内存差异也不能直接归因于数字解析。

其他代码事实：

- 先过滤、缓存 bitmap、int8 批量距离、float32 筛选和最终精算均已存在。
- 精算前 float32 筛选未拒绝的行会再次读取向量做补偿精算；重复读是否昂贵取决于拒绝率。
- 每查询有 distances/query codes、过滤 rows、堆和绑定表达式等临时对象；
  过滤 rows 按整个地址前缀 reserve，而非按25%匹配数 reserve。
- 单表缓存按修改/回滚失效；首次建立包括全前缀，不因 WHERE 只有25%而只加载25%。
- 树 `node()` 按值返回，encode/blob 有内容复制；这些操作存在，但其耗时占比尚未证实。

### 2.2 疑似瓶颈，必须测量后再排序

| 假设 | 支持线索 | 当前不能得出的结论 |
|---|---|---|
| int8 批量距离是暖查询大项 | 每条处理 m_c×D；历史 `row4-profile-10k.json` 的已列地址中 VNNI 样本合计27.00% | 历史二进制/源码与当前身份不同，且是部分地址表；不是当前阶段占比 |
| 保守界通过太多，精算仍多 | 全局最大误差、固定第二遍 cutoff、float screen 后可能再次全维精算 | 尚无当前通过率、R、阈值轨迹；不能认定界一定太松 |
| 分配和数据搬运成为固定成本 | rows/distances/q.codes/variant/RowView 分配和复制可定位 | 没有 allocator 计数和调用栈；不能将全部延迟差解释为 malloc |
| 协议往返掩盖局部 kernel 改善 | 每条同步请求，Python 编解码和管道在计时内 | 没有当前同负载的配对 executor/端到端分解 |
| 写入校验/树复制比数值解析更重 | 每行树访问、每批校验+fsync，数字解析优化无稳定端到端收益 | 可能还有同步/调度噪声；不能凭无收益反推 parser 占比为零 |

历史 [perf-write-profile.json](results/perf-write-profile.json) 只有临时插桩、
完全管道输入下 read/json/sql/exec 的汇总，没有足够身份/阶段条件支撑当前占比。
[row4-profile-10k.json](results/row4-profile-10k.json) 保留约4K cycles 样本及符号映射，
适合指导重新采样；不能拿其 `select` 未分解样本当作种子堆时间。

### 2.3 尚缺数据与已有脚本的口径问题

| 现有入口 | 已覆盖内容 | 缺口/限制 |
|---|---|---|
| `tests/benchmark.py:54` | 容器 T/B/M、ID 顺序、距离容差、写入重启内容 | 查询 T 排除前10条预热；首次缓存构建在其中。`preparation_ms`只计可选建索引，不计该缓存构建 |
| `tests/benchmark.py:93` | 1000条同步查询总时间 | 包含 Python 编码/解码、管道、调度、SQL执行和响应；没有单查询分布或阶段分解 |
| `tests/container_checks.py:18` | cgroup `memory.peak`、`io.stat` | 四次 `docker exec cat` 本身进入被测容器；至少首个采样进程可能影响 peak，影响量未知 |
| `tests/compare.py:11` | 保留设备原值并汇总读写字节 | 未认证专用设备归属，叠层设备不能盲目累计；page-cache命中的逻辑读取不等于物理读 |
| `tests/cold_start_benchmark.py:30` | 前20条、暖批次、重启及修改后重建 | “cold”是进程内缓存冷，不是文件页缓存被驱逐；启动到ready时间仍需单列 |
| `tests/warm_interleaved.py:24` | 单查询原始时延，可按奇偶拆过滤/未过滤 | 多版本进程共存改变主机缓存和资源竞争；不能当作严格隔离的正式测量 |
| `tests/executor_benchmark.cpp` | 同进程 parser+executor | 不含真实协议/输出；数据生成与查询数不同，不能与当前外部时间相减来估计通信开销 |
| `tests/{native_cgroup,system_cgroup}_benchmark.py` | Linux 原生 cgroup 外部读取、受限运行入口 | 需要对应Linux权限/服务；不是当前macOS上的原生amd64结果 |

**重要口径：**查询项 B/M 是该独立查询容器从启动、写入、装载/预热到查询的累计值，
不是仅1000条查询的增量。memory peak 包含文件缓存等 cgroup 记账，不能用进程RSS替代，
也不能把两个累计 peak 相减当作阶段 peak。因而相同6.531 MiB不证明查询没有逻辑读取，
也不证明磁盘是瓶颈。旧版写入首轮额外冷读的原值仍保留，不能只看中位数隐藏它。

当前缺少：所有阶段的当前时间占比、int8/float screen/精算次数、界拒绝率、
种子/最终堆更新和比较次数、分配/复制次数、当前逐查询分布及原生平台 PMU 结果。
现有 oracle 要求 ID/顺序严格相等，距离使用 `1e-5` 容差，
**不等于已经证明旧新距离逐位相同**。下一轮局部选择器实验应额外直接对照完整响应。

## 第三部分：优化机会排行榜

以下是**调查优先级**，不是已经测出的收益榜。高/中/低描述机会和代价，
未测项目的端到端百分比均无法可靠量化。“写入无直接变化”仍需检查查询后写入时的
缓存释放和总内存；本轮所有方向都只分析，不实施。

| 排名 | 方向 | 预期查询机会 | 写入影响 | 额外内存 | Exact风险 | 实现/验证成本 | 原生平台迁移 |
|---|---|---|---|---|---|---|---|
| 1 | int8 批量核/连续 VNNI 路径 | 中至高，取决当前核占比 | 无直接变化 | 可减少临时 rows | 低至中 | 中/中 | 强依赖CPUID，必须保留回退 |
| 2 | 更紧且动态的安全界 | R大时可能高 | 重建可能变慢 | O(C)或分块元数据 | 高 | 高/高 | 原理可迁移，收益依数据 |
| 3 | 有界查询 scratch 复用 | 固定开销高时中 | 保留容量可能挤压写入 | 受限保留 O(A+D+K) | 中，状态失效风险 | 中/中 | 较高 |
| 4 | 冷构建 reserve/装载减少复制 | 首次查询可能高，暖态低 | 修改后重建成本下降 | 目标降低构建瞬时峰值 | 中 | 中/中 | 较高 |
| 5 | 过滤 miss/选择率适配 | 谓词变化多时中，当前热态低 | 若增普通列索引则变慢 | 位图/列缓存需预算 | 中 | 中/中 | 依查询分布 |
| 6 | float筛选/精算成本与安全早停 | R高时中至高 | 无直接变化 | O(D)或小块状态 | 高，浮点并列 | 高/高 | 依CPU与数据 |
| 7 | 原始向量紧凑布局/AoSoA | 读原向量多时中 | 建缓存/持久布局维护成本 | 全量raw复制约4.883MiB/10k×128 | 中至高 | 高/高 | 依缓存/访存 |
| 8 | 起始种子堆 vs nth/短数组 | 可能低，必须先量占比 | 无直接变化 | nth pair额外O(m_c) | 低，需保持种子总序 | 低至中/中 | 相对较高但库/CPU有关 |
| 9 | SQL/响应对象复制与输出 | 固定成本高时中 | 解析写入也可能获益 | 目标减少分配 | 中，协议/生命周期 | 中/中 | 较高 |
| 10 | B+树 Node值复制减少 | 当前暖查询低，写入可能中 | 主要受益方向 | 可能减少短命副本 | 高，引用/淘汰/COW | 高/高 | 较高 |
| 11 | 序列化/小写入/提交校验融合 | 查询无直接收益 | 可能中，需先画像 | 有界缓冲/校验状态 | 高，事务恢复 | 高/高 | 依文件系统/同步成本 |
| 12 | 缓存外尾部按范围定位 | 当前10k低，大表可能高 | 无直接变化 | 低至中 | 中，遗漏边界 | 中/高 | 随规模变化 |

### 3.1 每项的证据、预测、反证和最小实验

**1. int8 批量核。**证据：`code_distances`全维处理有效行，历史采样提示该核值得优先重测；
AVX2连续路径已做，不能重复申报。减少：无过滤 VNNI 的 rows 分配/iota/索引读取，
或核内重复加载/水平归约。预测：若正式CPU走VNNI，核时间可能下降，写入基本不变；
当前无可靠加速百分比。退化：代码膨胀、短维/过滤 gather、AVX频率影响。
Exact：每个 int32 码距逐项相同、无溢出，安全界和最终精算不变。
反证：目标核没命中，或核变快而端到端不变。最小实验：先记录实际dispatch，
相同 codes/query/rows 离线对比 scalar/当前/单一改动，含尾维和极值。

**2. 安全界。**证据：当前小K第二遍用全局最大误差和固定cutoff，
float筛选在精确堆更新后另用新阈值。减少：不必要的原向量读取/float筛选/精算。
预测：误差差异大、后续堆继续收紧且R大时有利；无法可靠量化。
退化：逐行读errors、分支、重算界抵消省下的精算。
Exact：只在含全部舍入误差的严格界外拒绝，ASC/DESC/并列分别证明。
反证：R不降、或下降但时间反增。最小实验：先采通过率与阈值轨迹，再仅改一种界。
历史逐行误差原型三轮native加速比1.054739/1.008618/0.987261，已撤回；
该反例见 [nov16-optimization.md](nov16-optimization.md)，不是待集成的成功方案。

**3. scratch复用。**证据：每次新建distances、rows、q.codes和堆；
25%过滤仍为rows reserve整个A。减少：分配/清零/扩容，必要的数据重写仍要做。
预测：可降低分配次数，内存peak可能降低也可能因保留capacity升高，速度无法可靠量化。
退化：跨大表/小表保留容量、脏状态、allocator本已复用。
Exact：每查询重设有效长度，表/root/修改/失败语句/回滚均正确清理，不缓存错误的结果。
反证：分配减少但warm延迟或后续写入内存变差。
最小实验：先计容量/分配，再仅复用一个distances缓冲，不混入布局或堆替换。

**4. 冷构建。**证据：按row_limit预reserve后再紧缩复制，读取最多1024样本后遍历C。
减少：过量瞬时reserve和紧缩副本、可证明重复的记录读取。
预测：首条/重建延迟与peak可能下降，稳态通常无收益；无法可靠量化。
退化：减少reserve会造成多次扩容，新增计数扫描反而更慢。
Exact：不改变scale/error覆盖、完整地址集合和尾部回退。
反证：首条加速但装载I/O/peak或后续查询恶化。最小实验：只变一个容量策略，
固定同一数据目录副本，分别测ready、first、首次filter、warm、mutation后first。

**5. 过滤。**证据：已有四槽位图，当前仅两种谓词；首个miss仍扫描A行，
rows收集后才计算码距。减少：高churn下重复RowView/谓词扫描和不必要rows搬运。
预测：当前热两谓词收益可能很低，新谓词/低选择率才可能有利。
退化：额外索引维护、低复用缓存、低选择率随机取码，甚至比连续扫更差。
Exact：所有比较符、字符串/整数和事务可见性一致，不能以全局TopK替代过滤后TopK。
反证：miss稀少或按选择率分支的成本更大。最小实验：先拆miss/hit及m，
后续单独固定一个选择率比较位图枚举与当前路径；不本轮改变数据。

**6. float筛选/精算。**证据：float全维通过后再double补偿全维，存在双遍读取。
减少：低拒绝率时浪费的float遍历，或能安全证明失败行的剩余精算。
预测：由float拒绝率、R和D决定，不能假定普通SIMD精算是等价替代。
退化：早停分支破坏流水线，块级界计算比做完128维更贵；DESC不一定可用。
Exact：保留最终距离算法；若改早停，必须有针对该算法输出的外向舍入区间，
近界/非有限screen/不支持情形回退。不能仅比较普通partial sum。
反证：省维度但延迟变高，或任一near-tie结果变化。最小实验：只在隔离诊断版统计
float拒绝率和每块累积轨迹，再用离线trace评估，不立即替换精算核。

**7. 内存布局。**证据：codes row-major连续，原向量嵌在磁盘记录中、靠offset获取。
减少：原始向量精算的跨记录访问、load解码，或跨候选SIMD时的gather。
预测：仅R大且数据访存占比高时有利；全量float矩阵会多占约4.883MiB，
还要与int8缓存、pagecache和构建临时空间共同预算。
退化：冷构建复制、缓存污染、低选择率SoA跨行读取；高D/大N无法全缓存。
Exact：位模式复制、不改变累计顺序，任何未缓存行仍扫描。
AoS适合逐行精算/选择性过滤；SoA/AoSoA可能利于跨行批量核，但需要转置并测
块宽/尾部。反证：warm快但first或峰值代价更大。最小实验：先离线比较同一
refine行trace的row-major与小块AoSoA，不改持久布局或同时改距离算法。

**8. 起始种子选择。**证据：`database.cpp:843`可隔离，尚无耗时占比。
减少：堆更新和分支；nth同时增加构造/交换，短数组增加移动。
预测：K=10更可能是小收益或无收益方向，第三/四部分不预报百分比。
退化：nth的O(m_c)中间pair和移动、对阈值顺序的意外改变。
Exact：种子集合、种子复核顺序、last_seed、其余扫描全部保持。
反证：micro变快但端到端CI跨1、内存反增、或seed trace不同。
最小实验：按第四部分先micro，再决定是否值得唯一一次数据库局部替换。

**9. 解析/输出。**证据：多处字符串所有权、query复制、逐响应分配、获胜行RowView；
上一轮减少数字预扫描没有稳定端到端改善。减少：实际重复字节扫描、短命对象。
预测：固定开销高时有利，不能将parser micro收益直接外推到query/write。
退化：string_view悬空，SQL与JSON转义/非法UTF8的不同验证范围被误合并，
大结果spool行为变化。Exact：保持grammar、double→float、完整响应和错误恢复。
反证：分配减少却无端到端改善或边界失败。最小实验：先分parse/bind/output，
再仅消除一处有证据的复制，不跳过协议或字符校验。

**10. Node值复制。**证据：`node()` hit返回entry vector副本，insert路径再维护cache；
历史“语句新节点原地修改”原型Docker未稳定改善，已撤回。
减少：查找/更新复制和分配。预测：写入与树遍历可受益，暖编码查询通常不受益。
退化：LRU淘汰/递归插入使引用失效，额外pin/分支抵消收益。
Exact：老根不可变、语句保存点与回滚完整；不能把返回引用直接跨淘汰使用。
反证：副本明显减少但写入不快，或失败语句/树分裂出错。
最小实验：先计Node副本字节与分配，再只改只读find访问策略；不重启已失败的
更宽泛原型。与本轮种子实验分开。

**11. 写入复制与校验。**证据：encode→blob二次字符串、逐行pwrite、
每批checksum_region+fsync。减少：已证实的内容复制、小系统调用或重复校验读回。
预测：仅CPU/系统调用而非同步等待占优时有利；若fsync主导，上限有限。
退化：缓冲放大内存、导致read-your-writes不同；COW页最后字节在flush前尚可变，
随写增量checksum不一定等于最终字节序。
Exact：文件格式、checksum覆盖和顺序、确认点、回滚/恢复均保持，
不减少实际事务提交数、不推迟成功确认所需fsync。
反证：调用数减少但T/B/M无改善或恢复不一致。
最小实验：先测parse/encode/tree/checksum/sync，随后只消除blob一处复制，
再跑针对持久化的故障检查；本轮不实施。

**12. 大表尾部。**证据：缓存不完整时每查询`storage.scan(root)`，
虽然按ID跳过前缀，树entry仍从头遍历。减少：已知前缀的重复树遍历/Node副本。
预测：N超缓存时有效，当前10k不应有收益。
退化：范围seek开销、错误处理tombstone/边界或根变化。
Exact：按B+树全序定位严格后继，保留每个未缓存可见行，失败/回滚失效。
反证：大表tail成本低或seek不更快。最小实验：下一阶段只变N、记录A/C/tail，
再独立验证范围扫描；不与当前K=10选择器实验混跑。

## 第四部分：TopK 选择算法预测

### 4.1 先回答占比与收益上限

**当前种子选择占总查询耗时多少：未知，现有数据无法回答。**
这里的种子选择只指 `database.cpp:843` 的起始堆，不含int8点积、K次种子复核、
后续安全扫描、最终精确堆和输出。`select`整体采样不能代替这个占比。

若该阶段占端到端 p，阶段加速 s，则整体加速
`S = 1 / (1-p+p/s)`。即使阶段变成零耗时，最多 `Smax=1/(1-p)`。
例如 p=5% 时最多约1.053倍，p=10%时最多约1.111倍。
这些是**条件计算，不是已测占比或预报**。
若实测p很低，就应明确放低其优先级，不靠换算法名字追求5000分。

### 4.2 三种候选选择器

比较对象始终是相同的整数码距和扫描序号 rank；最终精确结果堆不改。
表中m_c在当前规模通常为无过滤10000、过滤2500。

| 项目 | 当前固定大小最大堆 | `nth_element` + 排序K项 | 有序固定短数组 |
|---|---|---|---|
| 复杂度 | O(m_c+UlogK)，最坏O(m_c logK) | 标准非并行算法平均O(m_c)，另有O(KlogK)种子顺序恢复 | O(m_c+UK)，最坏O(m_cK) |
| 大多数未入选项 | 一次rank与堆顶比较 | 参与分区比较/可能交换 | 可先与最差项比较后跳过 |
| 入选项 | pop/push，约logK层，分支依数据 | 分区交换pair，末尾K项再排序 | 插入点查找+最多K项移动 |
| 临时空间 | O(K)，vector可能扩容 | 常规pair原型额外O(m_c) | O(K)，K=10可固定栈空间 |
| 局部性 | 10项rank约160字节，易留在L1 | 额外较大连续pair区，构造与交换有流量 | 10项连续，移动可预测但次数更多 |
| 阈值影响 | 保持当前种子/处理顺序 | 必须恢复同一顺序才是单因素实验 | 同样保持原处理顺序 |

非并行 `nth_element` 的标准保证是**平均线性**，并非总是比小堆快或最坏线性。
Docker锁定GCC14.2；该版libstdc++使用introselect并有深度耗尽的heap选择回退，
应以实际库/CPU测量，不以算法名推断常数。

一个仅用于理解U的模型：如果rank独立、连续、扫描次序随机，
填满K以后预期替换数是 `K·(H_m−H_K)`，K=10时约69次（m=10000）、
55次（m=2500）。当前int8距离有ties、扫描按主键，未必符合该模型；
**不能把这些数填进实验结果**。应实际计U及比较/移动次数。

### 4.3 明确预测及推翻条件

1. **耗时预测：**保守预测为“整体不一定更快，可能相当或变慢”。
   K=10时小堆已很小，多数项不更新；nth增加物化和交换。
   若U特别高、堆分支代价突出，nth可能胜出；当前无法可靠量化百分比。
2. **内存预测：**以 `vector<pair<int32_t,size_t>>` 的可审查原型为准，
   典型amd64 pair为16字节，10000项约156.25KiB、2500项约39.06KiB，
   替代约K项小堆；须用该构建的`sizeof`确认。capacity/allocator/pagecache
   会影响cgroup peak，不能声称peak必按此字节精确增加。
   uint32索引原型约39.06/9.77KiB，但多间接比较，属于后续另一个因素。
3. **理论倾向：**当前K=10、m=2500或10000时，小堆或带最差项快速检查的短数组
   更有常数优势的可能；nth平均线性没有自动优势。K变大可能改变结论。
4. **结果相反时：**先检查输入rank/ties、U、库分区实现、分支和数据移动，
   再排除不一致的种子处理顺序、缓存状态、编译器和测量噪声。
   正确性trace一致后，才把实际胜出作为反证接受；不事后改写预测。
5. **应先microbenchmark。**只要完整纳入构造、分配、选择和K项顺序恢复，
   它可低成本淘汰更差方案；仅测已有数组上的nth kernel会漏掉主要新增成本。
   micro胜出仍需端到端确认，不能替代阶段占比。

### 4.4 Exact 等价条件

rank严格保持 `(descending ? -code_distance : code_distance, i)`，
其中i为当前有效码距序号，再通过同一`row_at(i)`映射。
不得用只有距离的比较器或把i误写成另一索引。码距有界，取负不碰INT32_MIN。

取 `k=min(K,m_c)`；空集合、m_c≤K分别处理，避免错误nth边界。
若用 `begin+k` 分区，前k项是所需集合，k==m_c不必分区；
之后按rank从大到小恢复当前堆pop顺序，并保存同一last_seed。
未选种子的cutoff遍历顺序、比较、refine调用、最终堆比较均原样保留。
这样才不会把“选择器”与“种子质量/精算顺序/阈值变化”混成一次实验。
除了最终ID/顺序/距离，还应逐查询核对seed集合、顺序与后续refine trace。

## 第五部分：最小成本性能测试计划

### 5.1 顺序和停止条件

| 步骤 | 工作 | 产物与门槛 |
|---|---|---|
| 0：身份/范围 | 保存5572448与当前生产源码/二进制、固定镜像/编译/数据/种子/query顺序；核官方目录不变 | 使用已有baseline/identity，不重跑无关全套 |
| 1：A/A噪声 | 同一二进制相邻交换标签运行少量完整批次，记录负载和顺序 | 先量环境可分辨的差异；宿主持续干扰则停止延迟排名，保留结果 |
| 2：画像 | 当前未改算法版本做低频采样；必要时独立诊断构建做阶段汇总和计数 | 得到p、U、R、筛选拒绝率及分配范围；先校准诊断开销 |
| 3：离线micro | 同一份当前码距trace比较heap/nth/短数组，构造开销计入 | 同rank种子/顺序全等；明显不更快则不集成 |
| 4：唯一局部实验 | 只有第2/3步支持才将起始堆换为nth，其他生产段无diff | trace与完整响应一致，128MiB安全 |
| 5：配对端到端 | 预先固定5个种子，各做A→B及B→A，10对完整批次 | 分过滤/未过滤/first/warm；若证据不足最多追加一次同规模，不无限重测挑最好轮 |
| 6：决定 | 用成对批次统计，兼看T/B/M与冷构建代价 | CI跨1或改善不超过A/A波动：报告“证据不足”；任何正确性失败立即停止并撤回 |

若诊断显示种子选择即使归零也不足以越过噪声/工程门槛，就停止集成选择器。
可预先采用“预计端到端收益至少1%且大于A/A波动”作为工程筛选门槛，
**这是决策规则，不是已测收益**。一次micro测试仍可用于学习和验证预测。

### 5.2 低开销画像，不把计数器当真实时间

优先在可用的原生Linux amd64上用采样器观察调用栈。先确认perf权限和PMU，
可从软件task-clock采样开始；cycles、instructions、cache-miss只能在真实可用时报告。
macOS `sample`/Instruments能观察本机调用栈，但ARM程序/模拟器的指标
不能直接当作原生x86 kernel的cycles或缓存表现。当前没有新采到的PMU结果。

若需埋点，仅在独立导出/诊断构建里添加，不进入默认生产路径、不改变stdout协议。
单线程计数无需atomic；每阶段做粗粒度时间汇总，避免每候选读时钟。
嵌套refine/float筛选/精算用排他时间或父子列表示，不能重复相加成“总占比”。
阶段包括：

```text
协议解析 → SQL parse → bind → cache构建 → filter构建/枚举 → query量化
→ int8批量距离 → seed选择 → seed复核 → 余行界检查/复核
→ 最终K排序 → 投影/序列化 → 响应写出
```

| 计数/测量 | 记录内容 |
|---|---|
| 有效候选 | A/C、m、coded eligible、地址未编码行、树尾部行，分有无过滤 |
| 量化距离 | 批量调用数、实际处理行数/维度，不把SIMD一次调用当一次候选 |
| 安全界 | int8检查/拒绝/通过，float检查/拒绝/通过/非有限回退；种子单列 |
| 精算 | refined_distance调用及D；若探查早停只记录块轨迹，不改输出 |
| TopK | seed填充、U、pop/push、比较/移动；final填充与V分别记录 |
| 过滤 | key命中/miss/超长回退、位图构建扫描数、有效行数 |
| 分配/复制 | 目标vector容量变化、allocator调用栈、明确位置的复制字节；不虚称覆盖所有分配 |
| 写入 | parse/convert/encode/blob/tree/checksum/sync/response时间与调用/字节数 |

全局operator new计数不覆盖所有malloc、mmap、SSO或编译器内联复制；
拦截memcpy也不等于全部复制。须声明统计范围，分配调用栈与阶段计时可分开运行。
比较诊断开/关同负载配对开销：若超过噪声，阶段时间只作定性参考，
正式延迟使用无埋点版；计数也需注意额外分支影响。

### 5.3 时间、I/O和内存的分段

- **时间：**启动到ready（包括恢复）；100批写入确认；可选索引准备；
  第一条无过滤（含缓存构建）；首次过滤（含位图miss）；随后暖查询总时间。
  逐查询分p50/p95/p99/max，过滤与未过滤分开，oracle与报告生成在计时外。
- **I/O：**保存每设备原始读写字节与阶段边界差分，专用设备映射确认后再汇总；
  数据准备、装载/预热、计时query、同步回写分别说明。
  查询独立试验可用相同已提交目录副本，准备成本单列；这不替代正式全流程口径。
- **内存：**从被测cgroup外部读，避免`docker exec`测量进程污染。
  同时保留全流程peak和阶段方法；若内核支持peak重置，核实其文件描述符语义，
  否则用独立匹配流程取得阶段peak，绝不相减累计peak。记录file/anon与OOM事件。
- **限制：**固定2CPU、128MiB、swap=0，不启动并发构建或本任务其他重活；
  不终止用户其他工作，不全局drop caches。进程缓存冷和文件页缓存冷分开标注。
  Docker模拟结果与原生Linux结果分别展示，不混成一组统计。

Linux接口依据：cgroup v2官方说明的memory.peak、memory.stat、io.stat。
具体内核是否支持reset、IO控制器是否委派，均需在执行环境检查；缺失值记缺失，
不用RSS或`/proc/PID/io`冒充同口径cgroup值。

### 5.4 Microbenchmark和统计

用当前相同数据生成的整数码距trace，覆盖真实无过滤/过滤两组；
另用全tie、单调、随机排列作为机制反例，不把它们混入主工作负载总分。
先做trace正确性，再做不含trace记录成本的独立micro。
主指标含rank构造、申请/释放、选择、恢复K顺序；kernel-only作为辅助。
用结果摘要防止编译器删掉工作；每组轮换选择器顺序，不同时跑其他性能任务。

端到端采用相邻A/B及交换顺序，使用相同种子/查询逐项对照。
主统计单位是**独立完整批次/种子**，不是把相关的1000条query当1000个独立重复。
展示每对时间、成对log耗时比中位数及区间；bootstrap按种子/批次分组，
小样本CI本身也不稳定。p95/p99描述每query尾延迟，不能代替总吞吐统计。
写入T/B/M是负面对照：选择器未改写入路径，若变化很大应先查环境/测量。
内存/IO不同阶段、不同日期的最好值不得拼接成“最佳版本”。

### 5.5 正确性与后续矩阵

下一轮先检查唯一改动的seed trace、refine顺序和完整JSON结果。
保留独立oracle的ID/顺序及距离容差检查，并额外直接对比旧新返回距离的数值、
必要时格式字节；若不同，必须解释，不能用容差掩盖无意舍入变化。
针对ties、近tie、极端float32、空过滤、少于K、m_c≤K、ASC/DESC、
缓存前缀/尾部、修改/失败语句/回滚/重启做选择边界覆盖。
再跑原样官方公开驱动和128MiB资源检查。文档调查本轮不重跑这些数据库测试。

后续实验分别改变单一因素：N=5万/10万；D=64/256/768；
K=1/50/100；选择率=1/10/50/100%；新谓词与反复修改；冷/热/连续多query；
原生amd64与模拟amd64。该矩阵全部待执行，不在当前选择器对照里同时展开。

## 第六部分：下一次建议修改

**唯一建议的算法对照：在诊断与micro支持的前提下，用
`nth_element + 对K个种子恢复原顺序`替换量化起始种子堆。**
理由是范围小、可逐项证明种子等价、能直接检验当前待讨论问题，
不是它已被证明收益最高。第四部分的pair物化内存成本必须计入。
最终精确堆、量化/float筛选界、refined_distance、WHERE、持久化和协议全部不动。

明确预期：m_c扫描仍存在，int8点积和剩余精算不会凭空减少；同种子、同顺序时，
R和最终结果应一致。可观察收益主要来自选择器自身，可能不足环境波动。
若micro不胜、p太低或端到端CI跨1，就保留现实现并记录反证，
随后再由实际画像从第三部分选择一个新瓶颈，不夹带其他修改。

正式性能分超过5000仍是待验证目标。平台基线、当前T/B/M分项和缩放口径不足，
不能把本地毫秒比、历史总分或自制代理直接换算成平台分。
用户预测和实验后解释尚未发生；报告、预测草案、执行结果与学生实际贡献应分别标明来源，
同一问题的预测/纠错/复测归同一实例，不新增虚构发言或学习得分。

## 核查来源

仓库源码和脚本均按本报告开头版本读取：

- [database.cpp](../minivectordb/database.cpp)、[database.h](../minivectordb/database.h)、
  [storage.cpp](../minivectordb/storage.cpp)、[storage.h](../minivectordb/storage.h)。
- [screen.cpp](../minivectordb/screen.cpp)、[screen.h](../minivectordb/screen.h)、
  [sql.cpp](../minivectordb/sql.cpp)、[protocol.cpp](../minivectordb/protocol.cpp)、
  [main.cpp](../minivectordb/main.cpp)、[Dockerfile](../Dockerfile)。
- [benchmark.py](../tests/benchmark.py)、[paired_round2.py](../tests/paired_round2.py)、
  [performance_round2.py](../tests/performance_round2.py)、[compare.py](../tests/compare.py)、
  [container_checks.py](../tests/container_checks.py)。
- [cold_start_benchmark.py](../tests/cold_start_benchmark.py)、
  [warm_interleaved.py](../tests/warm_interleaved.py)、
  [executor_benchmark.cpp](../tests/executor_benchmark.cpp)、
  [native_cgroup_benchmark.py](../tests/native_cgroup_benchmark.py)、
  [system_cgroup_benchmark.py](../tests/system_cgroup_benchmark.py)。
- [screen_bounds.cpp](../tests/screen_bounds.cpp)、[screen_stream.cpp](../tests/screen_stream.cpp)、
  [screening.py](../tests/screening.py)、[query_cache.py](../tests/query_cache.py)、
  [address_cache.py](../tests/address_cache.py)。

另核对非仓库的原始资料：C++工作草案 `[alg.nth.element]` 的非并行平均线性保证；
GCC `releases/gcc-14.2.0` 的 `libstdc++-v3/include/bits/stl_algo.h` 中
`nth_element/__introselect`；Linux内核官方《Control Group v2》内存/I/O接口。
这些资料用于核对算法保证和统计口径，不提供本项目当前速度或阶段占比。
