# 全文索引热路径优化（赛题四）

本次优化保留 IK/BENG/space 的分词规则、字符分类优先级、停用词、词频和 MATCH 评分语义。官方基准生成器、SQL、计时脚本、评分脚本和固定基线没有修改。

## 定位依据与修改

沿 `TOKENIZE / 索引构建 → parser → ObAddWord` 调用链检查，发现以下按字符或按词重复执行的操作：

1. `ObFTCharUtil::do_classify` 原来依次调用六种字符判断，每种判断各自解码同一个 Unicode 字符。现在解码一次，再按原顺序检查英文、数字、中文、其他 CJK 和 UTF16 代理项。不扩大字符集合，不改变错误返回。
2. `ObIKFTParser::process_one_char` 已取得当前字符、长度和类型，原来四个处理器又分别读取上下文。现在传递这些已验证的信息，保持处理器执行顺序和状态机不变。
3. `ObAddWord::groupby_word` 原来先查询计数，再覆盖插入，重复计算哈希、遍历桶；重复词还会分配和释放哈希节点。现在使用 `set_or_update`，在桶锁内更新。保留首次计数为 1、后续累加输入频次的既有行为，并保留排序规则等价但字节不同的词形处理。
4. `ObBasicEnglishNormalizer` 原来每个 token 都重用 arena 并重新申请输出空间。现在在 normalizer 内保留缓冲区，仅在容量不足时分配，reset/reuse 时清理。使用与 `ObCharset::tolower` 相同的 `casedn_multiply` 和 charset `casedn`，包括原来的原地转换和扩容转换分支。输出有效期仍为下一次 `get_next/reset/reuse` 之前，缓冲区不跨实例共享。

原版混合负载的 Linux perf 采样（199 Hz，3398 个样本）中，查询侧 BM25 batch eval 自身约 2.77%，字典 `ObArrayHashMap::find` 约 1.03%，`DATReader::match_with_hit` 约 0.52%。这些数据说明混合测试还包含大量查询工作，不能把分词热路径优化视为整个查询链路的同幅度提速。这里选择消除可直接确认的重复工作，没有修改 BM25 或倒排访问逻辑。

## 正确性验证方法

新增的 `unittest/storage/fts/test_fts_hot_path.cpp` 覆盖三个 UTF8MB4 排序规则下的字符分类边界、无效输入、不支持的字符集、词频聚合、排序规则等价词形和未初始化错误。seekdb 当前只注册 UTF8MB4 和 binary，未把尚未注册的 UTF16 当成可运行的测试。`unittest/share/test_english_normalizer.cpp` 对照现有 `ObCharset::tolower` 检查三个 UTF8MB4 排序规则和 binary 的大小写转换、长短 token 交替、缓冲区增长、reset/reuse 和标点裁剪。storage 测试接入现有 CMake 模块测试入口。

```bash
cmake --build build_release --target seekdb share_tests storage_tests -j3
build_release/unittest/storage/storage_tests
build_release/unittest/share/share_tests
```

实际执行结果为 storage 3/3、share 6/6 通过（share 含原有 4 项 AI schema 测试及新增 2 项 normalizer 测试）。

SQL 回归位于 `tools/deploy/mysql_test/test_suite/fts_index/{t,r/mysql}/fts_hot_path.*`，覆盖 IK smart/max_word、中英混合、全角与日韩字符、补充平面字符、重复词频、BENG token 长度过滤、索引构建后的 MATCH 结果与分数、布尔查询、LIMIT、插入/更新/删除/回滚及索引重建。预期结果来自未优化源码构建的实例。

`unittest/storage/fts/verify_sql_equivalence.py` 提供额外差分检查：208 篇固定种子的文档，分别使用 IK smart、IK max_word、BENG、space，合计 832 个 TOKENIZE；使用 `output=all` 比较完整 JSON（含词频），并在官方 20,000 行数据上比较四种 MATCH 的全部命中 ID 和原始相关性分数。它不修改测试库，在同一实例数据上切换二进制，避免只比较命中总数。

该差分检查实际比较 36,498 行，完全一致；结果 SHA256 为 `11d0423e549442122639d1688508e1a9240da688a3b833b323668764c4bfb635`。新增 `fts_hot_path` mysqltest 在原版及优化版均通过。

现有 FTS 套件共运行 16 项（含新增用例），原版与优化版均为 11 项通过、5 项失败，失败项没有被当成通过：`create_fts_index_afterward`、`simple_query`、`tokenize_function` 仅有空行差异；`partitioned_simple_query` 另有 EXPLAIN 参数编号 `:0/:1` 差异；`basic_dml` 查询内部表 ID 时出现 `No such row`。四份 `.reject` 在原版和优化版逐字节相同，`basic_dml` 在相同语句失败。既有 expected 文件保持原样。运行时仅将 Windows checkout 的 CRLF 转为 LF 的 expected 副本放在外部日志目录。

```bash
python3 unittest/storage/fts/verify_sql_equivalence.py \
  --client /path/to/obclient --port 2883 \
  --reference /tmp/fts-original.json --record --benchmark-database
# 原实例正常关闭后，用优化版启动相同数据目录，再执行：
python3 unittest/storage/fts/verify_sql_equivalence.py \
  --client /path/to/obclient --port 2883 \
  --reference /tmp/fts-original.json --benchmark-database
```

## 本地环境与性能复现

Windows 宿主机上的 Ubuntu WSL2，Intel Core i5-11260H（12 个逻辑 CPU），WSL 内存约 7.6 GiB。seekdb 使用 RelWithDebInfo、Clang 17、`-O2`、unity build、LLD；实例独占端口 2883，`memory_limit=4G`、`datafile_size=2G`、`datafile_maxsize=8G`、`log_disk_size=4G`。测试时不运行编译任务。

原版以 `9eb94e4ca` 为源码基准；优化版仅增加本文说明的内核修改。二者在同一个构建目录、相同编译选项下构建，使用相同数据目录交替执行完整测试。两次构建均使用相同的 WSL Rust 工具链兼容设置与脚本 LF 转换；这些本地构建环境处理未作为赛题内核修改提交。每次使用新的报告文件，并保留每轮原始指标，避免挑选最佳单轮。

```bash
export MYSQL='obclient -h127.0.0.1 -P2883 -uroot -N -s --default-character-set=utf8mb4'
export MYSQL_VERBOSE='obclient -h127.0.0.1 -P2883 -uroot --default-character-set=utf8mb4'
REPORT_FILE=$(mktemp /tmp/fts-bench-report.XXXXXX)
LABEL=local ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 \
  SAMPLES=3 WARMUP=30 SKIP_LOAD=0 OUTPUT="$REPORT_FILE" \
  bash tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py "$REPORT_FILE"
```

该命令删除并重建专用 `fts_large_bench` 测试库。每轮均包含重新导入和三个索引构建，未减少输入、执行次数或预热次数。

固定 CI 基线与本机环境不同；六轮中原版和优化版使用官方评分脚本均得到 100 分，这不能作为本次修改带来满分提升的证据。实际优化收益应使用同机原版与优化版比较，正式成绩仍由统一 CI 决定。报告中的 `git_head=unknown` 来自不含 `.git` 的 WSL 构建副本；[manifest.json](fts-performance-evidence/manifest.json) 记录源码基准、五个内核文件的原版/优化版 SHA256、两个可执行文件 SHA256 及官方基准文件 SHA256，以补充构建来源证据。

## 六轮完整测试结果

运行顺序为原版、优化版、优化版、原版、原版、优化版，各三轮；下表先对每项指标取各版本三轮均值，再计算耗时下降比例。所有六轮均保留在 [原始报告及日志目录](fts-performance-evidence/)，各轮数据与汇总在 [summary.json](fts-performance-evidence/summary.json)。正值表示耗时下降，负值表示退化。

| 计分指标 | 原版均值 | 优化版均值 | 耗时下降 |
| --- | ---: | ---: | ---: |
| build_ik_all_sec | 9.625000 s | 9.072667 s | 5.74% |
| build_ik_content_sec | 7.205333 s | 7.094333 s | 1.54% |
| build_beng_en_sec | 3.902333 s | 3.782333 s | 3.08% |
| tokenize_ik_avg_ms | 0.220667 ms | 0.213567 ms | 3.22% |
| tokenize_beng_avg_ms | 0.166500 ms | 0.163600 ms | 1.74% |
| query_cn_avg_ms | 2.067300 ms | 2.131800 ms | -3.12% |
| query_beng_avg_ms | 3.000333 ms | 3.051333 ms | -1.70% |
| query_mixed_avg_ms | 2.246833 ms | 2.376733 ms | -5.78% |
| query_limit_avg_ms | 2.589200 ms | 2.628667 ms | -1.52% |

按题目公式，构建类平均改善 **3.45%**，分词类平均改善 **2.48%**，查询类平均改善 **-3.03%**，综合改善 **0.97%**。若仅把同机原版均值作为参照代入评分公式，相当于约 **1.93/100 性能分**，不是正式 CI 成绩，也未达到耗时减半目标。该组数据只支持小幅局部收益，查询项出现退化，不能据此声称整体有稳定、显著的提速。原版与优化版多项单轮取值范围重叠，WSL 调度、缓存与后台存储任务的波动仍然影响结论。

### 时钟检查与无效数据排除

前期试跑发现 WSL `tsc` 时钟源下，实时时间约每 31 秒回拨约 1.3 秒，使官方脚本产生负耗时；这些试跑全部排除，未用于上表。未修改官方计时逻辑。六轮有效测试临时暂停 `systemd-timesyncd`，并将时钟源设为 `hyperv_clocksource_tsc_page`，在同一个 WSL 会话中运行测试，同时每 50 ms 检查 realtime 与 monotonic 的偏移。有效六轮共 126 组采样耗时均为正，未检测到超过 20 ms 的时钟跳变，检查记录为 `paired-clock-steps*.json`（空数组），完成日志为 `paired-runs-clockfixed.log` 和 `paired-runs-extra.log`。

测试结束已恢复原来的 `tsc` 时钟源并启动 `systemd-timesyncd`。该环境处理仅用于使本地官方计时可用，统一 CI 应使用其自身环境设置。
