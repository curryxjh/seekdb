# MiniVectorDB

基于 OceanBase `2026-competition-round2` 的独立 C++20 实现。存储、SQL
解析与执行、向量索引、事务和恢复均自行实现，没有复用 SeekDB 引擎，也没有
SQLite、RocksDB 或向量数据库依赖。

程序遵循官方 `2026-competition-round2` 的 JSONL stdin/stdout 协议。协议说明与
公开驱动 `protocol_check/run.py` 已合入本仓库，原样驱动和验证结果均保留。
公开 20/20 用例和正式平台准入均已通过，平台结果见下文。

**2026-11-11 19:58 正式提测通过 MySQL 准入。** 提测版本为
`b3b16af1e8429b3daed14bbdfedc89bf67c6e2a8`，平台性能原始指标为
`1968.948310`，Agent 原始分为 `1`。用户要求将性能指标提高到超过 `5300`。
本轮优化保留官方协议、驱动与公开断言；本地测量见
[性能迭代记录](docs/performance-iteration.md)。新版本尚未正式提测，不能将本地
耗时收益直接换算成平台指标。旧协议失败及修复历史见
[平台排查记录](docs/platform-failure.md)。

## 构建与运行

需要 Docker，构建时下载锁定摘要的 GCC 14.2.0 / Debian Bookworm 基础镜像。
运行时无需网络。镜像内程序是 Linux x86_64；Apple Silicon 上通过 Docker
模拟运行，性能记录不能直接代表原生竞赛机器。

```sh
docker build --platform linux/amd64 -t minivectordb:local .
docker volume create minivectordb-data
docker run --rm -i --platform linux/amd64 --network none \
  --memory 128m --memory-swap 128m \
  -v minivectordb-data:/data minivectordb:local <<'JSONL'
{"id":1,"op":"execute","sql":"CREATE TABLE items(id BIGINT PRIMARY KEY, tag VARCHAR(16), v VECTOR(2));","search_mode":"exact"}
{"id":2,"op":"execute","sql":"INSERT INTO items VALUES(1,'中文','[0,0]'),(2,'example','[3,4]');","search_mode":"exact"}
{"id":3,"op":"execute","sql":"SELECT id,tag FROM items ORDER BY id;","search_mode":"exact"}
{"id":4,"op":"execute","sql":"SELECT id,L2_DISTANCE(v,'[0,0]') AS d FROM items ORDER BY d LIMIT 10;","search_mode":"exact"}
{"id":5,"op":"execute","sql":"BEGIN;","search_mode":"exact"}
{"id":6,"op":"execute","sql":"UPDATE items SET tag='committed' WHERE id=1;","search_mode":"exact"}
{"id":7,"op":"execute","sql":"COMMIT;","search_mode":"exact"}
{"id":8,"op":"execute","sql":"CREATE VECTOR INDEX items_v ON items(v);","search_mode":"exact"}
{"id":9,"op":"shutdown"}
JSONL
```

正常关闭会先回滚未提交事务；再次运行并挂载同一个数据卷可以读取已提交内容。
所有数据、元数据、锁及匿名结果/排序临时文件都在 `/data`。不启动监听服务。

推荐使用 Docker 数据卷。已观察到 Docker Desktop/macOS 的两个独立主机目录
挂载不能可靠互斥文件锁；同一 Linux 数据卷的竞争进程锁检查通过。在原生 Linux
上，也可挂载具有可靠 `flock` 语义的本地目录。

本机构建只需支持 C++20 的 Clang 或 GCC：

```sh
./build.sh
./build/minivectordb --data /absolute/path/to/database
```

也提供 CMake 配置。地址/未定义行为检查与故障注入是独立的测试构建选项：

```sh
SANITIZE=1 FAULTS=1 OUTPUT=build/minivectordb-sanitize ./build.sh
```

## 官方 JSONL 通信接口

恢复完成后输出并刷新：

```json
{"type":"ready"}
```

输入是每行一个 JSON 请求，SQL 位于 `sql` 字段，模式位于 `search_mode` 字段。
每条请求输出一行严格 JSON，并原样回传 `id`：

```json
{"id":1,"ok":true,"affected_rows":2}
{"id":2,"ok":true,"columns":["id","d"],"rows":[[1,0],[2,5]]}
{"id":3,"ok":false,"error":{"code":"duplicate_key","message":"duplicate primary key"}}
```

普通 SQL 错误后可以继续下一条请求。关闭请求使用
`{"id":4,"op":"shutdown"}`，服务端返回成功后退出。stdout 仅输出协议报文并
逐条刷新，诊断写 stderr。`exact` 与 `ann` 目前都进行精确检索；`ann` 是精确回退，
没有宣称实现近似搜索。完整约定见 [STDIO_PROTOCOL.md](STDIO_PROTOCOL.md)，原样
公开驱动见 [protocol_check/run.py](protocol_check/run.py)。

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

本地防护上限为每条 JSONL 请求 16 MiB、catalog 4 MiB、4096 表、每表 1024 列、
类型参数 1–1048576；这些不是组织方冻结限制。整条 SQL 的解析树在内存中，
最大请求能否适合最终内存预算仍须按正式配置校准。

## 验证与实验

测试使用 Python 3 标准库，公开题包的预期结果未修改：

```sh
python3 tests/public.py --binary build/minivectordb
python3 tests/protocol.py --binary build/minivectordb
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
不将它包装成次线性 ANN。冻结配置、官方性能基线和新版本的平台准入结果仍待确认。

唯一学习材料是 `agent/conversation.json`。它区分用户原话、Agent 工作和工具
结果；当前没有用户对技术实验作出的解释或复盘，不把自动开发算作学生贡献。
