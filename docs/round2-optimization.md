# MiniVectorDB 本轮优化与交付

日期：2026-11-13，Asia/Shanghai。

基于 `origin/minivectordb-round2` 的完整提交
`12b8554683f8f3208e46d74ccf048d76a592c102`，在
`codex/minivectordb-round2-optimize` 开发。工作区开始时干净，旧本地
`codex/minivectordb-round2` 已按要求删除，原远端分支保留。

当前 `minivectordb/` SHA256：`3cd4d076b45ce782f426e2e4318f5b8dfb1750fbf7cc3fc60ad7fc8a82d8e04d`。
Linux 镜像 ID：`sha256:502e5cb83ac5c0f4c1c0117d81346e1619c8458841f287b02f213de52da81b8e`。

目标是完整功能准入和两榜满分，但当前版本没有正式提测。以下均为本地结果，
不能换算为平台原始指标、榜单名次或 Agent 分数。

## 最终测量

本机 ARM/macOS 三轮端到端查询加速中位数为 **2.985×**，约三倍。
Linux x86_64 在 Docker Desktop 上模拟运行，三轮配对查询加速中位数为
**1.564×**。环境差异、通信与容器开销不能忽略；
没有证据证明正式平台原始指标已提高三倍。

负载为 10000 条、128 维、每轮 1000 次 Top10，一半过滤到 25% 的记录；
写入每批 100 条自动提交。三轮分别计算，不拼接各指标最优值。
所有查询的 ID、数量、过滤、顺序及距离均对照独立 Python 真值，
两组 Recall 均为 1.0；写入核对完整内容和重启持久性。

### 本机端到端查询

| Seed | 原版本 ms | 新版本 ms | 加速 |
| --- | ---: | ---: | ---: |
| 20261112 | 206.715 | 69.255 | 2.985× |
| 39034843 | 208.857 | 67.783 | 3.081× |
| 774193 | 207.958 | 70.086 | 2.967× |

### Linux x86_64 容器完整统计

| 项目 | 指标 | 原版本三轮中位数 | 新版本三轮中位数 |
| --- | --- | ---: | ---: |
| 写入 | 端到端 ms | 1687.996 | 1685.120 |
| 写入 | 原始 I/O MiB | 6.906 | 6.906 |
| 写入 | 容器峰值 MiB | 15.426 | 14.730 |
| 查询 | 端到端 ms | 613.430 | 391.133 |
| 查询 | 原始 I/O MiB | 6.906 | 6.906 |
| 查询 | 容器峰值 MiB | 20.828 | 22.168 |

按逐轮三指标公式计算的本地代理值为 `0.543859`，不变版本的参考值是 0.5。
这不是官方 E、平台原始指标或排名得分。原始 io.stat 尚未认证专用设备与写回归属。
写入没有依靠省略同步提高成绩，I/O 也没有转移到 /data 之外。
缓存与量化表示构建发生在公开记录的预热过程中；其内存与 I/O 计入容器完整统计，
显式索引准备时间字段不包括隐式缓存构建。正式准备时间上限仍需组织方冻结配置验证。

完整数据：[本机](results/round2-native-delivery.json)、
[原容器版本](results/round2-final/baseline.json)、
[新容器版本](results/round2-final/candidate.json)、
[逐轮比较](results/round2-final/comparison.json)。其他 round2 探测报告为中间版本，
保留未获收益和不利结果，不能替代这些最终源码对应的报告。

## 核心改动

- 有界 int8 误差界筛选。保留原始 float32，只有保守界证明不会获胜时才排除；最终候选进行精确复核。
- 批量分派 SIMD，整数范数与整数候选阈值，稀疏谓词按位遍历。AVX2 配对乘积不会饱和；ARM 使用多条独立累加链。
- 精确距离使用补偿累加，保护并列与近并列顺序；投影只在表达式相同的情况下复用排序距离。
- JSON/SQL 普通字符串批量复制、ASCII UTF-8 快路、成功 from_chars 后避免重复扫描，同时保留严格转义、语法和溢出检查。
- 小结果不再初始化未使用的 16 KiB 文件缓冲；普通投影不再解码无关向量。
- 数据及临时排序文件分段同步，保留排序段清理残余脏页，防止页缓存超过容器预算。

算法与提交不变量见 [设计](design.md)。近似表示只用于安全剪枝，exact 不变；ann 仍为精确回退，
没有宣称实现次线性 ANN。缓存有界：原向量/偏移前缀 8 MiB，量化数据至多 3 MiB，
候选临时数组按这个前缀分配；更大数据流式访问。没有缓存查询答案。

## 修复资源准入问题

96 MiB、禁用 swap 的大事务测试中，原版本和第一版优化都在写入中以 137 退出。
先控制数据文件脏页后写入与索引通过，但外部排序仍以 137 退出；继续控制排序临时文件和
保留段的脏页后，全流程通过。记录中的 137 是实际退出码，已删除容器没有保留 OOMKilled 标志，
不把诊断推断冒充未采集的指标。

最终测试用 40000 条、142734352 字节已提交文件（约 136 MiB），在 96 MiB 限额下完成
大事务提交、索引、过滤 TopK、外部排序、批量更新/删除回滚、删除提交、未提交删除强杀和恢复。
提前刷页不发布 catalog，因此不改变事务提交点；超过 24 MiB 的未提交写入、
after_early_writeback 确定性强杀，以及大于 8 MiB 的失败语句也验证了原子性。
[失败及修复](results/round2-resource-iteration.json)、[最终资源](results/round2-writeback-resources.json)。

## 验证与材料

- 原样官方驱动公开 20/20、95 SQL 和协议边界通过，未修改官方断言。
- 生产与 ASan/UBSan：随机差分、Unicode/数值边界、缓存失效、回滚、向量真值、多层树分裂、强杀和重复恢复通过。
- 27305 组量化界检查，含 x86_64 实际 AVX2、批量/逐条一致性、极值、截断查询和 ASC/DESC。
- 新反例覆盖过滤后 LIMIT 大于候选数，以及 int8 并列但真实距离不同的 Top1。
[本机完整命令与输出](results/round2-validation-native.json)、
[容器完整命令与输出](results/round2-container/validation.json)。

唯一学习输入为 agent/conversation.json。材料保留学生真实预测、疑问、澄清后的解释、
迁移判断和实际结果；提供精确引用并按同一问题去重。历史平台质疑与本轮过滤、
刷页/提交、量化剪枝三条机制链分开。Agent 工作和引导不冒充学生自主贡献。
未提供的任务拆解、实验所有权和因果复盘不补写为学生发言；不自评原始分或预测排名。
[材料说明](../agent/README.md)。

## 复现

```sh
mkdir -p build/origin
git archive 12b8554683f8f3208e46d74ccf048d76a592c102 | tar -x -C build/origin
(cd build/origin && ./build.sh)
./build.sh
python3 tests/performance_round2.py --baseline build/origin/build/minivectordb --candidate build/minivectordb --output build/native-comparison.json
docker build --platform linux/amd64 -t minivectordb:round2-origin build/origin
docker build --platform linux/amd64 -t minivectordb:round2-final .
python3 tests/paired_round2.py --baseline-commit 12b8554683f8f3208e46d74ccf048d76a592c102 --output build/container-comparison
python3 tests/container_suite.py --image minivectordb:round2-final --output build/container-validation
python3 tests/container_checks.py --image minivectordb:round2-final --output build/resource-validation.json
python3 tests/linux_suite.py --output build/linux-fault-validation.txt
SANITIZE=1 FAULTS=1 OUTPUT=build/minivectordb-sanitize ./build.sh
python3 tests/large_transaction.py --binary build/minivectordb-sanitize --fault-writeback
python3 tests/evidence.py
```

正式限额、隐藏用例和新平台成绩仍未验证；没有自动正式提测或消耗提测机会。
