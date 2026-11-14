# 2026-11-14 首次查询优化

用户指出大规模首次查询仍更慢，并要求检查 Docker。以
`c9e3a50817305b901b9a222ca0adbfbce03f0469` 为本轮 baseline，继续使用
`minivectordb-perf5000`。平台最后一份用户反馈仍是更早版本性能 2300、Prompt
原始分 5；本轮未发起平台评测，也不据本地耗时换算 5000 分。

## 定位与实现

首查要构建有界量化前缀，先读取原始向量求全局最大绝对值，再量化编码。
一次隔离的 baseline 分段探测中，50000×128 首查约 52.7 ms，其中比例扫描
11.2 ms、量化 38.5 ms。这是定位用单次探测，不作为最终配对性能结论。

生产改动仅在 `minivectordb/screen.cpp`：

- AVX2 并行计算绝对值最大值；浮点输入、全局尺度及裁剪范围保持一致。
- 每次处理四个 float32，转换成 double 后计算量化码、整数范数及重构误差。
  先分离整数/小数部分，再按小数是否达到 0.5 进位，保留原有半整数远离零取整。
  不使用“加 0.5 再截断”，避免紧邻 0.5 下方的值被错误进位。
- 误差仍使用 double，保留绝对值舍入余量、64×D×epsilon 膨胀和向上 nextafter。
  四路正项部分和的舍入路径比原顺序累加更短；范数为可被 double 精确表示的整数。
  未使用 fast-math；尾部逐元素处理，无 AVX2 时回到标量实现。

两遍构建仍在第一次查询内完成，没有把工作延迟到第二次查询。缓存预算、未缓存
尾部扫描、低维路径、精确种子、最终补偿 double 距离、持久化及提交协议不改。
Dockerfile、公开协议、驱动和断言也没有改动。此优化不保证没有 AVX2 的机器加速。

## 原生 Linux 三轮配对结果

WSL2 Ubuntu、Linux x86_64、Intel i5-11260H、GCC 13.3。每个版本/种子使用新的
Linux /tmp 目录和 system transient service；MemoryMax=128 MiB、swap=0、
CPUQuota=200%、IOAccounting=yes。驱动与独立 Python float32/math.dist 真值在
数据库 cgroup 外。种子为 20261114、8675309、14223819，轮换版本顺序。

“首查”指进程内筛选缓存为空，不代表主动清空宿主页缓存。依次计时首查和后续
19 次查询，再测热查询批次；重启后重复，提交一次行更新使缓存失效后再次重复。
每次早期和热查询均检查精确 ID、顺序与距离；每轮最终核对全部标量行。
过滤和无过滤各占一半，K=10，每批写入 100 条并提交。

下表首查毫秒为各版三轮中位数；加速比是逐轮 baseline/candidate 比值的中位数，
因此不一定等于两个耗时中位数之比。小于 1 表示回退。

| 写入后负载 | baseline 首查 ms | 本版首查 ms | 首查加速 | 前 20 次累计加速 | 热查询加速 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 50000×128，无索引，热查询 200 次 | 60.700 | 25.049 | 2.412× | 1.639× | 0.931× |
| 100000×128，无索引，热查询 100 次 | 68.797 | 32.052 | 2.085× | 1.221× | 1.004× |
| 10000×128，有索引，热查询 500 次 | 11.308 | 4.433 | 2.549× | 1.547× | 0.997× |
| 10000×8，无索引，热查询 500 次 | 1.554 | 1.819 | 0.854× | 0.847× | 0.987× |

50000×128 重启后的首查中位数为 63.995 → 29.141 ms，配对加速 2.196×；
提交更新后的首查为 60.582 → 25.034 ms，配对加速 2.399×。
100000×128 超出前缀预算，仍扫描未缓存尾部；重启和更新后的首查配对加速分别
2.056×、2.051×。这没有改变线性最坏复杂度。

热查询不是本轮的稳定收益点：5 万行写入后的配对结果约慢 7%，1 万行索引在
更新后约慢 10%，其他阶段也有波动。8 维路径没有新增优化，其毫秒级首查出现回退，
不能宣称所有规模都更快。5 万行另与早期 cefca466 二进制比较：写入后首查约快
1.119×，重启后为 0.893×，仍比该早期版本慢；本轮主要解决相对 c9e3a508 的回退。

5 万行写入/查询进程峰值内存中位数为 44.52 → 44.46 MiB，10 万行为
77.86 → 78.09 MiB。保留原始 cgroup memory/io 计数；这些计数包含该进程的准备
阶段，不是纯查询增量，WSL 共享设备归属也不是组织方认证的 B 指标。

## 验证

原生 release 与 ASan/UBSan 检查通过，实际 argv、输出和退出码保存在
`docs/results/cold-native-validation.json`：

- 新量化检查：标量 2307 项、AVX2 2307 项，逐项比对码和范数，并用 long double
  核验实际重构误差。覆盖随机有限 float 位型、次正规数、最大有限值、零、正负
  半整数及相邻值、裁剪比例、短向量和尾维度。
- 保守量化距离界 27305 项；流式读取与 scalar/AVX2/VNNI 距离内核 5844 项。
- 协议、公开 20 用例/95 SQL、5 种子回归、缓存 41 项、向量数值 323 项、维度
  扫描 375 项、筛选 1176 项；故障与提交日志 124 项、大事务提前刷页/恢复通过。

## Docker 构建与实测

Docker Engine 29.4.0 可运行 Linux amd64 容器。两版都已通过仓库原 Dockerfile
构建，使用固定摘要的 GCC 14.2.0 和 Debian Bookworm 基础镜像。初次下载遇到
网络/TLS 超时，准备同一固定摘要的缓存并逐项核对镜像内容 SHA256 后，最终原始
构建命令成功；没有为测试改 Dockerfile、切换依赖版本或增加运行时网络访问。

本版测试镜像 ID 为
`sha256:3d0c619905ccb0904b0392ba8d35b2aadcfbd30ece0f6570098d9c2ee8ce29a2`，
镜像内程序 SHA256 为
`34fef50f1066570f12e8e0fe74a114f035e4a54a576d60b43f48c903a4d12212`。
两版构建输出和身份记录在 `cold-docker-*-build.txt`、`cold-docker-build.json`。

实际镜像的公开驱动 20/20、95 SQL，协议专项及八组容器回归均通过；独立 GCC
14.2 容器编译并通过了量化、距离误差界和流式/内核测试。
`cold-docker-validation.json` 保存了 14 条完成命令的实际输出与退出码，
`cold-docker-validate.py` 是实际执行脚本。

96 MiB、swap=0、network=none 下，40000 行提交后数据文件为 142734808 字节，
超过容器内存上限。大事务、索引、过滤 TopK、外部排序、更新删除回滚、删除提交、
未提交删除时 SIGKILL 和重启检查全部通过。强杀前 cgroup 峰值达到 96 MiB 上限，
重启阶段峰值为 80187392 字节；不能将“通过限制测试”写成“内存远低于限制”。
原始资源与 IO 计数保存在 `cold-docker-resource-96m.json`。

直接使用 Linux Docker CLI 连接现有 socket，50000×128、无索引、每阶段 500 次
热查询的三轮结果如下。限额仍是 128 MiB、swap=0、2 CPU，所有结果逐项核验。

| 阶段 | baseline 首查中位数 ms | 本版首查中位数 ms | 配对首查加速 | 前 20 次累计加速 | 热查询加速 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 写入后 | 93.810 | 34.462 | 2.892× | 1.758× | 1.048× |
| 重启后 | 88.059 | 33.773 | 2.648× | 1.762× | 1.049× |
| 提交更新后 | 105.492 | 37.623 | 2.820× | 1.739× | 1.040× |

`cold-docker-50k.json` 保存直接 Linux CLI 的全部三轮结果，包含容器传输时间。
最初还通过 Windows docker.exe 桥接跑了三轮，查询时间包含额外跨进程通信，且
热查询波动较大；原报告完整保留在 `cold-docker-windows-bridge-50k.json`，没有
按有利程度删除。其写入后配对首查/热查询比值分别为 2.040×、0.834×。
不同驱动路径及查询数的总时间不能直接相减归因，原生结果中的回退也仍然保留。

为减少顺序运行时的宿主漂移，另在同一对已预热容器之间逐条交错执行查询，逐轮
轮换先后和查询起点。单种子 20261114、9 轮各 200 个查询/版本，连同预热共核验
3640 次精确结果；配对加速中位数 1.011×，各轮为 0.994–1.027×，热查询基本持平。
该控制实验不替换上述三种子报告，也不证明其他负载均无回退。原始逐条耗时及
实际脚本在 `cold-docker-interleaved-warm.json`、`cold-docker-interleaved-warm.py`；
脚本按当时布局向候选目录上一级输出。

最后补强了可被 double 精确定位的 0.5 下方紧邻值断言，再用本机 release、
ASan/UBSan 和 GCC 14.2 容器复测。对应 8 条执行记录与最终测试文件摘要在
`cold-quantize-final.json`，脚本为 `cold-quantize-final.py`。

历史报告中的 Docker 不可用描述属于各自当时的环境，不重写为曾经通过本轮镜像验证。

## 复现与证据

基线与本版分别在干净 LF 检出目录执行 `sh build.sh`。原生模式需要 root/systemd：

```sh
sudo python3 tests/cold_start_benchmark.py \
  --baseline /absolute/baseline/build/minivectordb \
  --candidate build/minivectordb --output comparison.json
```

加 `--rows 100000 --queries 100`、`--rows 10000 --queries 500 --indexed`、
`--rows 10000 --dimension 8 --queries 500` 分别复现其他负载。容器使用同一驱动：

```sh
docker build --platform linux/amd64 -t minivectordb:cold-start-candidate .
python3 tests/cold_start_benchmark.py --backend docker \
  --baseline minivectordb:cold-start-baseline \
  --candidate minivectordb:cold-start-candidate --output docker-comparison.json
```

基线镜像也从 c9e3a508 的原 Dockerfile 构建。独立数值测试：

```sh
g++ -std=c++20 -O3 tests/screen_quantize.cpp -o build/screen-quantize
build/screen-quantize
g++ -std=c++20 -O3 tests/screen_bounds.cpp minivectordb/screen.cpp -o build/screen-bounds
build/screen-bounds
python3 tests/evidence.py
```

原始三轮报告为 `docs/results/cold-native-{50k,100k,10k-indexed,8d}.json`。
`cold-validate.py` 是实际执行的原生验证脚本，输出路径对应当时的候选目录布局。
源码 SHA256 为 `116808f592e7af4764ce345112ae420f0ea6416edadbf6e0856c11ee0dafd0be`，
原生 release 二进制 SHA256 为
`19adc169a211558459c7be27b206a21d852d3944e83abc51e4b8540705e65361`。

本轮实现、实验及分析由 Agent 完成；真实用户请求只记录为任务反馈，不扩充为
新的学生学习实例。学习材料保留原学生发言、实例及引用，历史工程 JSON 块仅压缩
空白，新增工程证据与最终源码同一次提交。平台新成绩、隐藏用例和排名仍待实际评测。
