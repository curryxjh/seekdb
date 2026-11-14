# 2026-11-14 性能回退修正

用户反馈上一提交 `c1d3ee02d9b5c90f9b74cfa9684a21746fbb508c`
性能 2300、Prompt 原始分 5；更早 `cefca4665498c5c20e1f85ca4201190e3434499c`
性能约 2500。这些均为用户提供的总分，没有 T/B/M 或平台日志，不能据此断定具体
回退原因。本轮从前一提交继续修改 `minivectordb-perf5000`，没有提交新平台评测。
5000 仍是未验证目标，已有 Prompt 分数也不是新材料成绩的保证。

## 改动与取舍

1. 对 32–4096 维移除常驻原始 float32 矩阵，只缓存行偏移、int8 筛选码、范数、
   重构误差和最多四个过滤位图。原始向量通过可回收的文件映射访问，实际候选才
   再次解码。构建分两遍，通过单行 scratch 查找尺度并量化。
2. 量化缓存总预算改为内存限制的 1/16，限制在 1–8 MiB；预算计算包含上述常驻结构。
   其他维度保留同预算的原始向量/行前缀（另有有界过滤位图），避免移除原路径造成回退。
   未缓存尾部仍逐条精确扫描，写入/失败语句/回滚继续失效缓存。
3. 先精确计算量化 TopK 种子的距离，建立真实候选堆；再用一个重构误差界筛选
   其他全部合格记录，替代围绕量化截止值的两倍误差界。种子通过总序去重，
   并列、过滤、降序和尾部都保留原精确语义，未把搜索改为近似 TopK。
4. AVX2 路径增加四行并行、共享查询加载；VNNI、ARM 和标量回退保留。
   独立测试直接逐项比较 scalar/AVX2/VNNI，不依赖运行时只选中其中一种。

提交协议、持久化校验、官方协议与驱动、Dockerfile 均未改动。本轮没有写入机制的
新增优化，写入时间变化只作为观测，不归因于这些查询改动。

## 最终源码三轮对照

环境为 WSL2 Ubuntu 原生 Linux x86_64、Intel i5-11260H、GCC 13.3。
三版本分别为 original=cefca466、previous=c1d3ee02、candidate=本轮源码。
每个版本/种子使用新的 transient system service 和 Linux /tmp 目录，
CPUQuota=200%、MemoryMax=128 MiB、swap=0。固定种子 20261114、8675309、
14223819，轮换执行顺序。Python 驱动和独立 float32/math.dist 真值在数据库组外。

| 行数 / 维度 / 索引 / 查询数 | 相对 previous 查询加速中位数 | 相对 original 查询加速中位数 | previous → candidate 查询阶段峰值 MiB 中位数 |
| --- | --- | --- | --- |
| 10000 / 128 / 无索引 / 1000 | 1.018× | 1.191× | 15.25 → 10.26 |
| 10000 / 128 / 有索引 / 1000 | 0.949× | 1.263× | 20.76 → 15.76 |
| 50000 / 128 / 无索引 / 200 | 2.351× | 3.420× | 56.26 → 43.84 |
| 10000 / 8 / 无索引 / 500 | 1.044× | 1.180× | 4.05 → 3.82 |

每批写入 100 条并提交，K=10，查询各半为无过滤和普通列等值过滤。
所有计时查询检查精确 ID、顺序、距离，过滤与无过滤 Recall 均为 1，重启后核对
全部标量内容和前四个向量查询。查询 T 在首次查询及 10 次预热后测量；冷查询单独
记录。M 和 I/O 包括该组的启动、写入准备、可选索引、首次查询与预热，不能当成
纯查询增量或把不同轮次最佳指标拼接。

本轮 system service 能读取 `io.stat`，区别于上一轮缺失 IO controller 的用户服务。
保存了原始设备统计；当前版与 previous 的写入字节及文件大小基本相同。
宿主共享设备/缓存的归属仍不是组织方认证环境，不计算官方 E 或换算平台分数。

1 万行计时收益较小，带索引负载三轮中出现相对 previous 的热查询回退；不能只报告早期较好的探索结果。
5 万行中，新版缓存覆盖更多记录，热查询改善但冷查询仍比 previous 更慢；首次
构建成本不能忽略。低维没有明显加速，带索引构建耗时也没有优化。更大数据超出
8 MiB 前缀时仍扫描尾部，不能由这里的结果外推亚线性复杂度。

探索阶段曾出现 8 维单轮查询约 266 ms 对 previous 202 ms 的回退，随后将
非量化路径移出混合候选回调；单轮复测约 199 ms 对 203 ms。最终结论使用上表
三轮最终源码报告，早期探索二进制结果不冒充最终结果，也不以单轮代替三轮。

## 验证与证据

- 官方公开驱动 20/20，95 SQL；协议、5 种子回归、缓存 41 项、数值、维度扫描、
  筛选 1176 项、三种插入顺序的树分裂检查通过。
- 故障构建的强杀、重复恢复、提交日志 124 项及大事务原子性检查通过。
- ASan/UBSan 构建的协议、公开用例、回归、缓存、数值、筛选、树和恢复检查通过。
- 量化边界 27305 项增加精确种子界断言，覆盖混合尺度、极值、次正规数、裁剪查询；
  新流式/内核检查 5844 项覆盖随机行顺序、四行边界和尾维度，两者 sanitizer 通过。
- 96 MiB、swap=0 下 40000 行大事务、索引、外部排序、回滚和 SIGKILL 恢复通过；
  32 MiB 下缓存尾部获胜用例 41 项通过。

当前 Docker API 不可用，本轮未启动修复或清理操作，实际镜像构建/运行仍未验证。
原生 GCC 13.3 与 Dockerfile 固定 GCC 14.2 不同；历史镜像结果不作为当前通过证据。
当前功能测试也不能代替隐藏用例、真实断电或平台复评。

原始记录均随源码同次提交：

- `docs/results/recovery-system-10k.json`
- `docs/results/recovery-system-10k-indexed.json`
- `docs/results/recovery-system-50k.json`
- `docs/results/recovery-system-8d.json`
- `docs/results/recovery-validation-results.json`（每条实际 argv、环境、输出和退出码）
- `docs/results/recovery-native-resource-96m.json`
- `docs/results/recovery-public.json`
- `docs/results/recovery-validate.py`（实际执行的验证脚本）

性能报告包含最终源码和二进制 SHA256，绝对路径为当时运行位置，不是运行依赖。
同一提交中 `agent/conversation.json` 保留所有原学生学习证据，并以 Agent 署名追加
本轮验证；只压缩三份未被学习实例引用的历史 JSON 报告空白，不新增学习实例或自评分。

## 复现

干净 Linux 检出本分支；源码保持 Git 的 LF。另准备 cefca466 和 c1d3ee02 两个
独立检出目录，分别执行 `sh build.sh`。当前目录也执行 `sh build.sh`。
systemd system transient service 需要 root，内存及 IO controller 必须可用：

```sh
sudo python3 tests/system_cgroup_benchmark.py \
  --original /absolute/original/build/minivectordb \
  --previous /absolute/previous/build/minivectordb \
  --candidate build/minivectordb --output comparison.json
```

加 `--indexed` 复现索引负载；加 `--rows 50000 --queries 200` 复现较大负载；
加 `--dimension 8 --queries 500` 复现低维检查。
完整验证脚本原件对应本地候选目录布局：基线在 `../../perf5000/baseline`，
结果输出至候选的父目录；其他布局需替换其中基线/输出路径。单独的新测试：

```sh
g++ -std=c++20 -O2 tests/screen_stream.cpp -o build/screen-stream
build/screen-stream
python3 tests/query_cache.py --binary build/minivectordb
python3 tests/evidence.py
```
