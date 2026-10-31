# 全文索引调优：实测与更正

本次同机四轮完整测试的综合耗时下降 **9.01%**：构建类 **2.72%**、分词类 **3.58%**、查询类 **20.72%**。这是有限的实际收益，尚未达到综合耗时减半的满分目标。

## 更正先前证据

2026-10-31 核查发现，之前外部启动脚本覆盖传入的 `BIN`，反复运行同一个旧二进制。先前“六轮对照”、0.97% 改善及第一次 SQL 差分不能证明提交版本的行为，相关结论撤回。旧文件在 [证据目录说明](fts-performance-evidence/README.md) 中标记无效，仅保留追溯。用户提供的正式评测为 -7.1358%、性能分 0，不能用之前本地报告否定该结果。

以下结论仅使用 [verified-v3](fts-performance-evidence/verified-v3/summary.json) 中的新记录。每次启动都核对 `/proc/<pid>/exe` 与请求路径，记录实际二进制 SHA256，测试结束再次核对路径。原版为 `9eb94e4cad24c57b9e99172f128ae8cb550f4f31` 的内核代码；保留了原分支已有的其他功能。

## 瓶颈依据与实现

对真实的上一版候选内核进行 Linux perf 采样，查询样本约 6,000 个：通用归并的 `push` 自身 3.50%、`fill_merge_heap` 3.49%、`collect_dims_by_id` 2.76%、过滤 3.46%、double 转布尔 2.07%、投影 2.15%。原版 LIMIT 查询另采样约 12,000 个，包含大量 binary 文档 ID 的通用字符集比较，以及倒排列表跳转。采样有链接器代码折叠造成的符号别名，不能将所有显示的容器符号直接视为独立热点。分词采样还包括字典冷加载，不能将 DAT 构建当作每次调用的成本。原始采样摘要和查询执行计划随证据保存。

主要修改如下：

1. IK 字符分类只解码一次 Unicode，四个处理器复用调用方已得到的字符、长度和类型；分类顺序、字符集合及状态机保持原样。
2. 词频聚合使用单次 `set_or_update`，避免重复哈希、查找和节点分配。保留首次计数为 1、后续累加传入频次，以及排序规则等价词保留最新词形的行为。
3. BENG normalizer 在实例内复用大小写转换缓冲区，只在容量不足时扩容。继续使用原字符集的 `casedn`，reset/reuse 清理状态。
4. TOKENIZE 无额外参数时直接使用现有默认属性；自定义参数校验后直接生成解析器属性，消除序列化后重新解析同一配置的过程。所有五种解析器均核对默认属性和显式空参数的输出。
5. 不超过三个词项的自然语言/布尔 DaaT 检索直接选择最小文档 ID，避免通用归并器的逐文档 push/pop。相同 ID 按原维度顺序累加分数；较长查询继续使用原通用路径。
6. binary VARCHAR 文档 ID 使用字节比较，保留 null 顺序、长度顺序和范围边界；其他元数据沿用原比较器。BMW 跳转在缓存批次内保留相邻访问路径，对较长跳转使用有序查找。
7. 仅在向量化执行、过滤器恰为相关性 double 的 BOOL 时直接做零值判断；其他谓词继续求值。批量投影使用框架的 `locate_datums_for_update` 初始化写缓冲区，并批量设置求值标志。

没有删除 LIMIT 查询的相关性排序，也没有把“先找到 20 个任意命中”当成 top-k。共享排序实现未在本次修改。

## 正确性

`storage_tests` 5/5、`share_tests` 6/6 通过。覆盖字符分类、错误输入、词频、等价词形、缓冲区增长/reset/reuse、默认属性，以及 binary/UTF8MB4 比较器的全配对结果和最小/最大边界。

`unittest/storage/fts/verify_sql_equivalence.py` 在相同数据目录切换原版和候选版，比较 **882 条 SQL、258,355 行结果**，逐字一致：842 条 TOKENIZE 比较完整 JSON 和词频，40 条 MATCH 比较全部命中 ID、原始分数及确定性排序。覆盖 1/2/3/更多词项、分数条件、布尔条件、LIMIT/OFFSET、五种解析器默认参数及配置参数。结果 SHA256：

```
4f4f4ff5d25bcd3dcc827c9bb61cee36250a07466610ae727efa24e9a11a0f84
```

FTS mysqltest 共 16 项，原版与最终候选均 **11 项通过、5 项失败**，通过/失败名单一致。新增 `fts_hot_path` 通过，包含分词、建索引、原始查询、布尔查询、分数过滤、LIMIT/OFFSET、DML、回滚和索引重建。五个失败没有记作通过：`create_fts_index_afterward`、`simple_query`、`tokenize_function` 有空行差异；`partitioned_simple_query` 另有 EXPLAIN 参数编号差异；`basic_dml` 在内部表 ID 查询时出现 `No such row`。前四项的 reject 文件原版/候选字节哈希一致，后一项在相同语句失败。详见 [correctness.json](fts-performance-evidence/verified-v3/correctness.json) 及两版日志。

## 性能对照

Ubuntu WSL2，Intel Core i5-11260H，12 个逻辑 CPU，WSL 内存约 7.6 GiB；两版同一构建目录、RelWithDebInfo、Clang 17、`-O2`、unity、LLD，AVX2 OFF。专用实例端口 2883，`memory_limit=4G`、`system_memory=1G`、`datafile_size=2G`、`datafile_maxsize=8G`、`log_disk_size=4G`。计时期间没有编译或 perf 采样任务。

四轮顺序为原版、候选、候选、原版，每版两轮，全部纳入平均。每轮使用新的报告文件，执行未修改的官方脚本：`ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 SAMPLES=3 WARMUP=30 SKIP_LOAD=0`，包含重新导入和三个索引构建。先分别求每项两轮均值，再按题目的三类等权公式计算提升，未将九项直接平均。

| 计分指标 | 原版均值 | 候选均值 | 耗时下降 |
| --- | ---: | ---: | ---: |
| build_ik_all_sec | 9.61500 s | 9.20950 s | 4.22% |
| build_ik_content_sec | 7.46750 s | 7.26400 s | 2.73% |
| build_beng_en_sec | 3.89950 s | 3.85150 s | 1.23% |
| tokenize_ik_avg_ms | 0.22920 ms | 0.22460 ms | 2.01% |
| tokenize_beng_avg_ms | 0.16565 ms | 0.15710 ms | 5.16% |
| query_cn_avg_ms | 2.02155 ms | 1.49585 ms | 26.00% |
| query_beng_avg_ms | 2.98640 ms | 2.14430 ms | 28.20% |
| query_mixed_avg_ms | 2.28600 ms | 1.67015 ms | 26.94% |
| query_limit_avg_ms | 2.65655 ms | 2.61050 ms | 1.73% |

四轮查询计数均为中文 8001、英文 11000、混合 7332、LIMIT 20；完整正确性依据仍是上面的逐行差分。分词、构建的改善较小，可能受 WSL 调度、缓存和存储后台任务波动影响；不能把查询的改善幅度套到所有链路。

只以同机原版作为参照代入评分公式，相当于 **18.02/100** 性能分。仓库固定基线比本机原版慢，两版用仓库评分脚本均得到 100，因此这些原始评分日志不代表本次优化拿到正式满分。正式评测的实际基线未提供，本地改善不能换算为正式成绩。

WSL 的 `tsc` 曾引发 realtime 回拨。有效对照使用 `hyperv_clocksource_tsc_page`，临时停止 timesyncd，并每 50 ms 检查 realtime/monotonic 偏移；四轮 84 组计时均为正，未记录超过 20 ms 的时钟跳变。官方计时逻辑未改。测试完成后恢复原 `tsc` 和 timesyncd。

## 复现与来源

```bash
cmake --build build_release --target seekdb storage_tests share_tests -j3
build_release/unittest/storage/storage_tests
build_release/unittest/share/share_tests

export MYSQL='mysql -h127.0.0.1 -P2883 -uroot -N -s -A --default-character-set=utf8mb4'
export MYSQL_VERBOSE='mysql -h127.0.0.1 -P2883 -uroot -A --default-character-set=utf8mb4'
REPORT_FILE=$(mktemp /tmp/fts-bench-report.XXXXXX)
LABEL=local ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 \
  SAMPLES=3 WARMUP=30 SKIP_LOAD=0 OUTPUT="$REPORT_FILE" \
  bash tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py "$REPORT_FILE"
```

基准会重建专用 `fts_large_bench` 数据库。切换二进制时应正常关闭实例，保留同一数据目录，并核实实际进程路径。差分脚本先用原版 `--record --benchmark-database` 写新 reference，再用候选版去掉 `--record` 比较。

[manifest.json](fts-performance-evidence/verified-v3/manifest.json) 记录全部 17 个修改内核文件的 LF 源码哈希、原版/候选二进制 SHA256 和五个官方基准文件的哈希。WSL 构建副本的生成版本号陈旧，来源核对以实际源码和二进制哈希为准。原始四轮报告、日志、启动身份和时钟记录均保留，未修改基准数据、SQL、计时、评分脚本、基线或 CI 工作流。
