# 全文索引优化：clean-v22 阶段记录

本分支从 `5bce9e06d` 开始，只移植全文索引相关改动。用户指定排除的五个其他赛题提交不在分支祖先中；`0134d900e76c832a309bb8867aaabb951618dd18` 中有用的 FTS 修改按源码移植。

## 修改及瓶颈

- 建索引时逐 posting 的表达式求值、投影和排序比较重复较多：普通非分区在线回填按文档批量输出 postings；复用分词和行缓存；重复前导排序键按字典秩分组，保持完整排序语义。分区路由、隐藏表替换仍保留逐行路径，覆盖离线 DDL 与无主键表重建。
- mini 模式一个 DDL worker 串行执行可独立的正向和倒排辅助索引：资源允许时将普通在线 FTS 正向辅助索引放入独立队列；分区、隐藏表与离线重建继续原队列。
- IK 每文档重复建立内置只读词典视图：在词典 hub 中保留带缓存句柄的视图，首次初始化同步发布，其后只读复用。逐字符分类减少重复解码，固定 processor 直接调用，仲裁保留原有顺序。
- 分词中的重复 hash、大小写转换分配和 BENG 临时缓冲：合并词频更新、复用 normalization 缓冲；未发生大小写变化的 IK token 借用当前文档；保留 collation、词频与最后拼写语义。
- MATCH 合并逐行比较和投影成本高：短查询批量游标、16 字节二进制 doc ID 比较与投影、StringDiff 批量解码。只需要集合成员关系的 COUNT/常量投影免算 BM25；依赖 score、列或 HAVING 的路径保持评分。评分路径保留浮点累加顺序。

## 完整性能测量

Ubuntu/WSL，同一机器、编译方式和实例参数，TCP 2883，memory_limit=4G、datafile_size=2G、datafile_maxsize=8G、log_disk_size=4G。原版和候选使用同一启动脚本。官方脚本与基线文件逐字节核对（换行归一化后）未变。每轮均重新载入 20,000 行，batch=500、rounds=3000、query_rounds=200、samples=3、warmup=30、skip_load=0。没有扣除 SELECT 1 开销。

| 指标 | 原版 clean-base-4 | 候选 clean-candidate-22 |
|---|---:|---:|
| build_ik_all_sec | 9.928 | 4.705 |
| build_ik_content_sec | 8.180 | 3.049 |
| build_beng_en_sec | 4.459 | 1.845 |
| tokenize_ik_avg_ms | 0.3228 | 0.2079 |
| tokenize_beng_avg_ms | 0.2125 | 0.1768 |
| query_cn_avg_ms | 2.3729 | 0.6100 |
| query_beng_avg_ms | 3.3434 | 0.8043 |
| query_mixed_avg_ms | 2.5970 | 0.6210 |
| query_limit_avg_ms | 2.8399 | 0.3041 |

构建/分词/查询类别均值提升为 57.99% / 26.20% / 78.90%，三类等权综合为 **54.36%**。这是一次相邻完整对照。较早的 clean-base-3 对同一候选为 **44.23%**，因此尚不能声称稳定达到 50%。随附原始报告、分组时间、二进制身份和时钟检查，后续需要重复交替测量。仓库固定基线下两者均能得到 100 分，不能用它代替同机原版对照。

## 正确性

- storage/share 单元测试全部通过，覆盖字符边界、仲裁、词典匹配、属性解析、UTF8 大小写、doc ID、StringDiff 批量解码和 BENG 原有分词行为。
- 17 项 FTS mysqltest 原版与候选状态一致：12 项通过；5 项原有 fixture 失败（包含元数据查找和结果文件空行）。逐项比对失败输出，仅允许 simple_query 中一个预期的 COUNT EXPLAIN 从 calc_relevance=true 改为 false；其他输出一致。
- 新增 `fts_ddl_postings` 从原版实际执行生成 expected，候选通过。独立覆盖已有数据分区表在线创建 IK/BENG 索引、完整 ID/score、分区查询、更新删除、离线改列、无主键表 ADD AUTO_INCREMENT 后检索。
- 原版离线 DDL 用例 43.596 秒，候选 41.271 秒通过。
- 相同索引布局下原版/候选完整差分：388,723 行 TOKENIZE JSON/词频/MATCH ID/原始 score、395 行聚合、361,800 行共享排序结果完全一致。完整回填正确性另由上述独立 DDL 测试覆盖。
- IK/BENG/普通 B-tree 并发创建，期间 UPDATE/DELETE，Python 独立计算预期，逐 ID 核对通过。

可在同一测试实例分别运行原版与候选，通过 `unittest/storage/fts/verify_*_equivalence.py --record` 记录原版再比较候选；先运行官方完整 benchmark 建立 fts_large_bench。记录和比较 MATCH score 应使用同一索引布局，避免采样估计差异。所有脚本仅用于验证，不参与官方计时。

`manifest.json` 记录源码与二进制 SHA256；构建树的生成 revision 可能陈旧，因此使用实际源码哈希与运行进程 exe 路径核对身份。此次是用户授权的中间推送，后续继续优化和验证稳定性。
