# MiniVectorDB

基于 OceanBase `2026-competition-round2` 的独立 C++20 实现。存储、SQL
解析与执行、向量索引、事务和恢复均自行实现，没有复用 SeekDB 引擎，也没有
SQLite、RocksDB 或向量数据库依赖。

当前使用 **`minivectordb-local-v1` 本地接口**。公开题包没有提供完整的组织方
通信驱动、冻结协议和资源配置，不能据本地通过结果声称正式 29 个用例通过、
正式协议兼容或取得比赛名次。

## 构建与运行

需要 Docker，构建时下载锁定摘要的 GCC 14.2.0 / Debian Bookworm 基础镜像。
运行时无需网络。镜像内程序是 Linux x86_64；Apple Silicon 上通过 Docker
模拟运行，性能记录不能直接代表原生竞赛机器。

```sh
docker build --platform linux/amd64 -t minivectordb:local .
docker volume create minivectordb-data
docker run --rm -i --platform linux/amd64 --network none \
  --memory 128m --memory-swap 128m \
  -v minivectordb-data:/data minivectordb:local <<'SQL'
CREATE TABLE items(id BIGINT PRIMARY KEY, tag VARCHAR(16), v VECTOR(2));
INSERT INTO items VALUES(1,'中文','[0,0]'),(2,'example','[3,4]');
SELECT id,tag FROM items ORDER BY id;
SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM items ORDER BY d LIMIT 10;
BEGIN;
UPDATE items SET tag='committed' WHERE id=1;
COMMIT;
BEGIN;
DELETE FROM items WHERE id=2;
ROLLBACK;
CREATE VECTOR INDEX items_v ON items(v);
SQL
```

EOF 正常退出；再次运行并挂载同一个数据卷可以读取已提交内容。未提交的显式
事务在 EOF、SIGTERM、SIGINT 和重启恢复时回滚。所有数据、元数据、锁及匿名
结果/排序临时文件都在 `/data`。不启动监听服务。

推荐使用 Docker 数据卷。已观察到 Docker Desktop/macOS 的两个独立主机目录
挂载不能可靠互斥文件锁；同一 Linux 数据卷的竞争进程锁检查通过。在原生 Linux
上，也可挂载具有可靠 `flock` 语义的本地目录。

本机构建只需支持 C++20 的 Clang 或 GCC：

```sh
./build.sh
./build/minivectordb --data /absolute/path/to/database --mode exact
```

也提供 CMake 配置。地址/未定义行为检查与故障注入是独立的测试构建选项：

```sh
SANITIZE=1 FAULTS=1 OUTPUT=build/minivectordb-sanitize ./build.sh
```

## 本地通信接口

恢复完成后输出并刷新：

```json
{"ready":true,"protocol":"minivectordb-local-v1"}
```

输入是以分号结束的 SQL 流，支持多行、注释、引号内分号、双写引号和反斜线
转义。EOF 也会执行最后一条完整但没有分号的 SQL。每条语句输出一行严格 JSON：

```json
{"ok":true,"affected_rows":2}
{"ok":true,"columns":["id","d"],"rows":[[1,0],[2,5]]}
{"ok":false,"error":"duplicate_key","message":"duplicate primary key"}
```

普通 SQL 错误后可以继续下一条语句。stdout 仅输出结果并逐条刷新，诊断写
stderr。输出故障或发布提交后无法确认目录同步时终止进程，不再输出误导性的
“已回滚”响应。

独立控制行 `\mode exact` / `\mode ann` 返回 `{"ok":true}`。
CLI 也接受 `--mode exact|ann`。目前两者都进行精确检索；`ann` 是明确的精确
回退，没有宣称实现近似搜索。公开题包中的 `--restart` 和 `--mode ann` 由
测试驱动转换，不是 SQL，也不是组织方协议的实现依据。

## 支持范围

- `CREATE TABLE`、`INSERT`（多行、可列出全部列）、`SELECT`、`UPDATE`、`DELETE`。
- 一个 `BIGINT` 主键，普通 `BIGINT` / `VARCHAR(n)`，最多一个 `VECTOR(n)`。
- 有符号 64 位整数、合法 UTF-8、按 Unicode 码点计长和排序、float32 向量。
- 普通列投影、`SELECT *`、`L2_DISTANCE`、`AS`、五种比较、`AND`、
  单个 `ORDER BY [ASC|DESC]` 和 `LIMIT`。排序并列按主键升序。
- `BEGIN`、`COMMIT`、`ROLLBACK`，自动提交、读自己的修改、失败语句原子性、
  多表提交、事务内建表和建索引。
- `CREATE VECTOR INDEX name ON table(vector_column)`，物理索引随增删改维护，
  提交、回滚、恢复后保持同一可见版本。

不支持 NULL、JOIN、聚合、子查询、ALTER、主键更新、OR 或多列排序，明确返回
错误。向量拒绝 NaN、Infinity、溢出、非数值及维度不符；L2 对外返回欧氏距离。
过滤先于 TopK，结果不足 K 时返回全部。

本地防护上限为每条 SQL 16 MiB、catalog 4 MiB、4096 表、每表 1024 列、
类型参数 1–1048576；这些不是组织方冻结限制。整条 SQL 的解析树在内存中，
最大请求能否适合最终内存预算仍须按正式配置校准。

## 验证与实验

测试使用 Python 3 标准库，公开题包的预期结果未修改：

```sh
python3 tests/public.py --binary build/minivectordb
python3 tests/regression.py --binary build/minivectordb
python3 tests/storage_splits.py --binary build/minivectordb
python3 tests/public.py --binary build/minivectordb-sanitize
python3 tests/regression.py --binary build/minivectordb-sanitize
python3 tests/storage_splits.py --binary build/minivectordb-sanitize
python3 tests/crash.py --binary build/minivectordb-sanitize
python3 tests/linux_suite.py --output docs/results/linux-fault-suite.txt
python3 tests/container_suite.py --output docs/results
python3 tests/vector_sweep.py
python3 tests/container_checks.py --output docs/results/container-resources.json
python3 tests/benchmark.py --output docs/results/opt-packed-scan.json
python3 tests/benchmark.py --indexed --output docs/results/opt-packed-postings.json
python3 tests/compare.py --baseline docs/results/opt-previous-scan.json \
  --candidate docs/results/opt-packed-scan.json --output docs/results/opt-final-comparison.json
python3 tests/evidence.py
```

故障测试必须使用 `FAULTS=1` 构建；生产 Docker 镜像没有环境变量控制的故障注入。
容器测试使用 Linux 数据卷、断网和禁用 swap，结束后自动移除本轮数据卷。
独立 Python 模型用于事务随机差分和 float32 检索真值，不调用 C++ 关键逻辑。
三指标比较器按同种子逐轮计算后取中位数；它只比较本地版本，原始 I/O
尚未认证设备与写回归属，输出不是官方 E 或排名。历史实验文件保留原版本
记录，最终版本与结果的对应关系见验证记录。

本轮默认扫描在三轮同负载对照中，1000 次查询耗时中位数从 1803.68 ms
降至 1412.45 ms，减少约 21.7%；原始 I/O 减少约 2.1%，写入阶段峰值内存
中位数减少约 12.9%。查询内存变化很小，不据此宣称稳定改善。环境为
Apple Silicon 上模拟的 Linux x86_64；这些是本地结果。

实际结果与性能取舍见 [验证记录](docs/validation.md)，存储不变量、失败边界和
资源设计见 [设计说明](docs/design.md)，完整状态见
[需求核对表](docs/requirements.md)。

持久化文件采用追加与写时复制，尚无压缩/空间回收；反复修改与大量 tombstone
会增长磁盘并增加扫描成本。精确扫描和物理向量扫描的查询复杂度仍为 O(ND)，
不将它包装成次线性 ANN。正式驱动、冻结配置、官方基线和授权平台评测仍待补齐。

唯一学习材料是 `agent/conversation.json`。它区分用户原话、Agent 工作和工具
结果；当前没有用户对技术实验作出的解释或复盘，不把自动开发算作学生贡献。
