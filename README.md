# MiniVectorDB

单机、单进程、单客户端的磁盘向量数据库。Python 实现 SQL 解析、记录存储、哈希索引、事务、提交日志和向量索引；NumPy 2.2.6 仅用于通用数组运算。支持数据大于内存预算时的流式查询和磁盘排序。

## 仓库与版本

- 上游起点：`oceanbase/seekdb` 的 `2026-competition-round2`，`f4d4e26653af823a7601668802f80e374ad6643c`。
- 提测目标：`https://github.com/curryxjh/seekdb.git`（本地 `origin`）；组织方为 `upstream`。
- 按用户要求，开发分支改名为 `round2-dev`。题目仍指定正式分支 `2026-competition-round2`，正式提交前须按平台接受的名称提供同一个已验证 commit；当前开发分支名不表示平台规则已变更。
- 代码、Dockerfile、设计说明及 `agent/conversation.json` 必须来自同一次提交。学习材料仅含真实来源记录，不能把 Agent 的解释当作学生的技术贡献。

## 构建与启动

正式目标为 Linux x86_64。基础镜像锁定 Python 3.12.8 的镜像摘要，NumPy wheel 随仓库交付并由 `requirements.lock` 校验 SHA256。基础镜像就绪后，安装和编译步骤可完全离线完成：

```bash
docker build --network=none -t minivectordb:local .
docker run --rm -i --network=none --read-only \
  --cpus=2 --memory=2g --memory-swap=2g \
  -v minivectordb-data:/data minivectordb:local
```

若默认 registry 无法访问，可使用同摘要镜像：

```bash
docker build --build-arg BASE_IMAGE=public.ecr.aws/docker/library/python:3.12.8-slim-bookworm@sha256:2199a62885a12290dc9c5be3ca0681d367576ab7bf037da120e564723292a2f0 -t minivectordb:local .
```

镜像直接以前台进程运行，不暴露端口，不启动网络服务。存储、事务暂存、索引和磁盘排序文件均位于 `/data`。不要使用 `-t`。本地源代码也可运行：

```bash
python -m minivectordb --data-dir ./data
```

无 NumPy 时源代码保留流式精确查询路径；正式镜像固定安装锁定的 NumPy 版本。

## stdin/stdout 协议与示例

指定分支的 `STDIO_PROTOCOL.md` 和实际题包驱动要求 **JSON Lines**，与背景文案中的纯 SQL 描述有差异。程序完成日志恢复及磁盘索引重建后，主动输出 `{"type":"ready"}`。每次请求仅产生一行 JSON 响应并立即 flush；日志仅写 stderr。

以下每行依次输入；请求中的 SQL 不应直接作为裸文本发送：

```json
{"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, tag VARCHAR(16), embedding VECTOR(2));"}
{"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1,'初始','[3,4]'),(2,'保留','[0,0]');"}
{"id":3,"op":"execute","sql":"SELECT id,tag FROM docs ORDER BY id;"}
{"id":4,"op":"execute","sql":"CREATE VECTOR INDEX docs_vec ON docs(embedding);"}
{"id":5,"op":"execute","sql":"SELECT id,L2_DISTANCE(embedding,'[0,0]') AS distance FROM docs WHERE tag='保留' ORDER BY distance LIMIT 10;","search_mode":"exact"}
{"id":6,"op":"execute","sql":"BEGIN;"}
{"id":7,"op":"execute","sql":"UPDATE docs SET tag='已提交' WHERE id=1;"}
{"id":8,"op":"execute","sql":"COMMIT;"}
{"id":9,"op":"execute","sql":"BEGIN;"}
{"id":10,"op":"execute","sql":"DELETE FROM docs WHERE id=2;"}
{"id":11,"op":"execute","sql":"ROLLBACK;"}
{"id":12,"op":"shutdown"}
```

重新运行相同 `docker run` 命令、复用 `minivectordb-data` 卷，再输入：

```json
{"id":"restart-check","op":"execute","sql":"SELECT id,tag FROM docs ORDER BY id;"}
{"id":"done","op":"shutdown"}
```

查询应返回 `[[1,"已提交"],[2,"保留"]]`。`shutdown`、stdin EOF 和 SIGTERM 均丢弃未提交事务；强杀通过日志恢复处理。提交成功响应发出前已对提交日志完成 fsync。

## SQL 范围

- `CREATE TABLE`：一个 BIGINT 主键，可使用列内或表级 PRIMARY KEY；最多一个 VECTOR 列。
- `INSERT INTO ... [(columns)] VALUES (...) [, ...]`，每行须提供全部列。
- `SELECT *` 或列投影、`AS` 别名、`WHERE` 比较 `= < <= > >=`、`AND` 和条件括号、单列或多列 `ORDER BY ... ASC/DESC`、非负 `LIMIT`。
- `UPDATE ... SET column=literal [, ...] [WHERE ...]`、`DELETE FROM ... [WHERE ...]`。
- `L2_DISTANCE`、精确过滤 TopK，距离并列按主键升序；普通排序也以主键升序作为最终并列规则。
- `CREATE VECTOR INDEX name ON table(vector_column)`，`exact` 与 `ann` 均返回精确结果；ANN 请求采用更强的精确语义，不以降低召回率换速度。
- `BEGIN`、`COMMIT`、`ROLLBACK`，同时至多一个显式事务；事务内可见自己的修改。失败语句独立回滚，之前成功的语句仍可继续提交。

BIGINT 使用有符号 64 位整数；VARCHAR 按 Unicode 字符数限制长度，按码点比较。向量在写入和查询常量解析时转换为有限 float32，距离使用转换后的值，以 float64 累加后开平方。不支持 NULL、JOIN、聚合、子查询、ALTER、主键更新、嵌套事务及其他未实现语法，均返回错误，不能部分解析后执行。

错误响应含 `ok:false`、非空 `error.code` 和消息；常见语义码有 `duplicate_key`、`type_error`、`invalid_vector`、`unknown_table`、`unknown_column`、`unsupported_sql`、`syntax_error`。题包当前没有冻结错误码映射，若组织方发布正式映射须对应更新。底层存储 I/O 故障会终止进程，避免在提交状态不确定时继续追加日志。

## 验证

Linux 下运行题包原始驱动：

```bash
python3 protocol_check/run.py --image minivectordb:local --handshake-only --output protocol-results/handshake-001
python3 protocol_check/run.py --image minivectordb:local --output protocol-results/public-001
python3 -m unittest discover -s tests -v
python3 scripts/stress_docker.py --image minivectordb:local --output local-results/stress-001
python3 scripts/benchmark.py --rows 10000 --dim 128 --queries 1000 --index --output local-results/benchmark-001.json
```

WSL 未开启 Docker 集成时，可以将 `scripts/windows-docker` 加到 PATH，使用转发到 Docker Desktop 的 CLI 适配器；题包本身不需修改。Windows 直接运行扩展测试可用 `python -X utf8 -m unittest discover -s tests -v`。官方驱动使用 POSIX 管道选择和进程组，应在 Linux/WSL 运行。

已执行的检查与边界见 [验证记录](docs/VALIDATION.md)；存储、恢复和索引设计见 [设计说明](docs/DESIGN.md)。公开测试及本地烟测通过不代表全部 29 个正式用例通过。未发起正式提测，也未取得正式性能或 Agent 评分。
