# 全文索引性能优化（2026-competition）

基于 `oceanbase/seekdb` 的 `2026-competition`（`5bce9e06d`）。目标覆盖 `tools/benchmark/fts_large_bench.sh` 的三类指标：索引构建、`TOKENIZE` 分词、`MATCH` 查询。所有修改均不改变分词结果、索引内容、查询结果与相关度分数。

## 瓶颈依据

在 macOS ARM64 本地 Release 构建上，用官方基准灌入 20,000 行数据，再用 `sample` 对单条 SQL 循环执行采样。

| 路径 | 热点（占该路径 CPU） | 原因 |
| --- | --- | --- |
| IK `TOKENIZE` | `ObFTRangeDict::find_first_char_range` ≈13% | 每个 CJK 字符都线性扫描词典分段，比较走 `ObCharset::strcmp`，每次都做参数校验和字符集分派 |
| IK `TOKENIZE` | `ObGenDicLoader::get_dic_loader` 写锁 ≈7% | 每行都对全局 map 加排他锁，只为取出已存在的 loader |
| IK `TOKENIZE` | `is_unicode_cn_number` ≈4% | 每个中文字符都线性扫描 34 项数组，外加静态初始化守卫 |
| 分词公共路径 | 词频 map 的 bucket latch ≈5–9% | `ObFTWordMap` 默认带读写锁，但每个实例只在一次 `segment()` 内单线程使用 |
| BENG `TOKENIZE` | `ObStopWordChecker::check_stopword` ≈18% | 每个词都新建 arena、做字符集转换、查带锁的哈希集合 |
| BENG `TOKENIZE` | `ObAddWord::groupby_word` ≈28% | 先 get 再 overwrite set：计算两次哈希，并重新分配节点 |
| `MATCH` DaaT | `ObSRMergeCmp::cmp` ≈6%，`filter_on_demand` ≈6% | docid（`VARBINARY`/binary）比较走通用 `strcmpsp`；`BOOL(relevance)` 过滤逐行求值表达式 |
| `MATCH` top-k（BMW） | `ObTextRetrievalDaaTTokenIter::advance_to` | 跳表推进时逐个 docid 走通用比较函数 |
| 索引构建 | 每个辅助表构建都固定 `sleep 100ms`；构建并行度为 1 | `report_ddl_sstable_checksum` 报告成功后仍无条件睡眠；FTS 辅助表 DOP 取 `MAX(p/3,1)`，默认 `p=1` |

## 修改

| 文件 | 修改 |
| --- | --- |
| `storage/fts/dict/ob_ft_range_dict.*` | 用内联的字节序比较替代 `ObCharset::strcmp`（`utf8mb4_bin` 即 memcmp 后比长度）。初始化时校验各分段有序且互不重叠，满足时用二分查找，否则保留原线性扫描 |
| `storage/fts/dict/ob_gen_dic_loader.cpp` | 先在读锁下查找已存在的 loader：map 只增不删，handle 的引用计数是原子操作。找不到时才走原来的写锁路径 |
| `storage/fts/utils/unicode_utils.cpp` | 中文数字判断改为对码点 `switch`，字符集合不变 |
| `data_plane/fts/ob_fts_struct.h` 及 3 个调用点 | `ObFTWordMap` 改用 `NoPthreadDefendMode`。哈希函数、相等判断、桶数、插入顺序都不变，迭代顺序因此也不变 |
| `storage/fts/ob_fts_stop_word.*` | 停用词：utf8mb4 下的纯 ASCII 词，按 `utf8mb4_general_ci` 的等价规则（去尾随空格、A–Z 折叠）直接比较。非 ASCII 或其他字符集仍走原路径。词频：一次查找后原地更新 key 和计数，等价于原来的“用最新 key 覆盖同一位置节点” |
| `storage/retrieval/ob_sparse_daat_iter.*` | binary docid 比较内联（含 NULL first，结果规范为 -1/0/1）。识别出过滤条件就是 `BOOL(relevance)` 时，直接判断 `relevance != 0`，等价于 `BOOL(double)`。结果投影改为一次定位整批 datum |
| `storage/retrieval/ob_text_retrieval_token_iter.*` | `advance_to` 中的 binary docid 比较内联 |
| `storage/ddl/ob_ddl_common_storage_impl.cpp` | 校验和是通过 inner SQL 同步写入的，报告成功即已落盘可见。因此只在全部重试都失败时才 sleep 100ms 退避 |
| `sql/resolver/ddl/ob_fts_index_builder_util.*` | 未指定任何并行度（取值为 1）时，FTS 辅助表构建并行度取 `min(max(cpu/2,1),4)`。显式的 hint、强制 DOP 或表 DOP 仍按原规则 `MAX(p/3,1)` |

## 正确性

- 比对方法：在**同一份数据**上分别启动原版和优化版，比较 1068 项结果，两者完全一致（SHA-256 相同）。比对项包括：
  - `TOKENIZE`：ik（默认、smart、max_word）、beng、space、ngram，每种都测默认输出和 `output:all`。输入覆盖中英混合、全角、日韩文、emoji 与补充平面字符、停用词大小写与尾随空格、超长文本、重复词，以及 80 条固定种子的随机混合文本。
  - `MATCH`：9 条查询（含 BOOLEAN MODE），比对全部文档 id 与分数（保留 9 位小数）、COUNT、按分数排序的 top-20，以及 `LIMIT 20`。
- 用优化版并行构建的索引查询，结果与用原版构建的索引一致。
- 开发过程中发现并修正过一处差异：词频原地更新起初保留的是第一次出现的 key，而原逻辑保留最后一次出现的写法（例如 `general_ci` 下互相等价的 `èê` 与 `éè`）。现已改为与原逻辑一致。

## 性能（本地 A/B，同一台机器，原版与优化版交替各跑 2 轮）

参数与 CI 基线一致：`ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 SAMPLES=3 WARMUP=30`。

| 指标 | 第 1 轮提升 | 第 2 轮提升 |
| --- | ---: | ---: |
| build_ik_all_sec | 21.3% | 24.8% |
| build_ik_content_sec | 16.6% | 26.4% |
| build_beng_en_sec | 19.9% | 29.9% |
| tokenize_ik_avg_ms | 7.8% | 10.0% |
| tokenize_beng_avg_ms | 2.8% | 5.5% |
| query_cn_avg_ms | 9.2% | 10.8% |
| query_beng_avg_ms | 9.3% | 13.3% |
| query_mixed_avg_ms | 5.1% | 10.9% |
| query_limit_avg_ms | -8.3% | -1.6% |
| **三类等权平均** | **9.5%** | **14.4%** |

说明：

- 本机 `SELECT 1` 约 0.08 ms，`TOKENIZE` 约 0.12 ms，SQL 框架开销占了单次调用的大部分，所以分词改进在本机被稀释。CI 基线中分词表达式本身的耗时占比要高得多。
- `query_limit` 在基准中波动明显。单独对这条 SQL 循环 200 次、重复 3 组，两版耗时一致（约 1.24 ms），没有观察到稳定退化。
- 正式成绩以 Linux CI 为准。本地结果只用于同机前后对比。

## 复现

```bash
./build.sh release --init --make
# 分别部署原版与优化版实例，然后执行：
LABEL=after ROWS=20000 BATCH=500 ROUNDS=3000 QUERY_ROUNDS=200 SAMPLES=3 WARMUP=30 \
  OUTPUT=/tmp/after.txt bash tools/benchmark/fts_large_bench.sh
python3 tools/benchmark/fts_large_bench_score.py /tmp/after.txt
```
