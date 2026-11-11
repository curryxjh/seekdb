# MiniVectorDB Round 2 开发工作区

## 仓库与分支

- 上游：`https://github.com/oceanbase/seekdb.git`
- 起点：`2026-competition-round2`，commit `f4d4e26653af823a7601668802f80e374ad6643c`
- 提测代码目标：`https://github.com/curryxjh/seekdb.git`（本地 `origin`）
- 当前开发分支：`round2-dev`，按用户要求改名；保留目标仓库原有分支。
- 本地 `upstream` 指向组织方仓库，默认推送目标为 `origin`。

题目附件第 4.1、4.4 节仍指定正式分支名 `2026-competition-round2`。当前开发分支名不是平台分支规则已更改的证明。正式提测前须核实平台是否支持选择 `round2-dev`；若仅接受固定名称，应将同一已验证提交发布至要求的分支，不覆盖已有远端工作。

## 当前状态

2026-11-11：已完成题包拉取、开发分支和远端配置、协议核对与真实学习记录初始化。**数据库尚未实现，不能正式提测。** 上游根目录 `Dockerfile`、`conversation.json` 均为空；本项目的有效学习记录使用题目指定的 `agent/conversation.json`。

当前题包含 20 个公开样例；公开通过不代表全部正式准入通过。容器引擎目前不可连接，未执行 Docker 构建、SQL 用例、性能或恢复测试。

## 协议依据

优先核对 `STDIO_PROTOCOL.md`、`protocol_check/run.py`、`protocol_check/_transport.py` 和当前公开用例。用户提供的背景说明称不再使用 JSON 封装，但指定上游提交中的文档及驱动实际使用 **JSON Lines**：

1. 恢复完成后主动输出 `{"type":"ready"}` 并立即 flush。
2. 每行接收一个含 `id`、`op` 的 JSON 对象；`execute` 携带 SQL 与 `search_mode`。
3. 每个请求仅回复一行 JSON，保留 `id` 值及类型，`ok` 为布尔值；日志只写 stderr。
4. `shutdown` 回滚未提交事务、完成清理，回复成功后以 0 退出。

此处为 Agent 对当前题包的核对结果，不代表学生已经解释或验证了该机制。协议仍须以组织方后续冻结配置为准。

## 开发顺序（计划，尚未执行）

1. 先实现可交互的 JSONL 入口，验证主动 ready、逐请求 flush、错误后继续、shutdown 退出。上游 `example_stdio.py` 仅为骨架，其 SQL 固定返回 `NOT_IMPLEMENTED`。
2. 实现 SQL 子集、类型与约束校验。BIGINT 保持有符号 64 位精度；VARCHAR 按 Unicode 字符计数、按码点比较；向量先验证并转为有限 float32。拒绝不支持的语法。
3. 实现磁盘记录布局、有界缓存、普通查询和精确向量扫描。过滤应先于 TopK；距离为欧氏距离，并列按主键升序。设计不能依赖全量数据驻留内存。
4. 自行实现语句原子性、显式事务及持久化提交。失败语句不污染之前的事务状态，已确认提交不依赖正常退出才落盘。
5. 增加重启、强杀、恢复中再次强杀测试，交叉验证普通查询与向量检索看到相同版本。
6. 基础正确后实现并评估自研索引、维护与 ANN 路径；exact 模式始终保留精确语义。依据测量决定优化，禁止预存答案和使用现成数据库或向量索引引擎代执行。
7. 锁定依赖、补齐从源码构建的 Dockerfile，在 Linux x86_64 和实际资源约束下复测。

## 后续自测与提交

题包驱动依赖 POSIX 管道选择及进程组操作；完整自测应在 Linux/WSL 或相应开发环境进行。正式镜像以前台 stdin/stdout 模式启动，数据、日志、索引和临时文件统一位于 `/data`，正式执行不联网。完整数据库和镜像实现后运行：

```bash
docker build -t minivectordb:local .
python3 protocol_check/run.py --image minivectordb:local --handshake-only --output protocol-results/handshake-001
python3 protocol_check/run.py --image minivectordb:local --output protocol-results/public-001
```

这些命令当前只是后续操作说明，未执行且不表示通过。不得放宽题包参数后声称满足正式要求。

源码、可构建 Dockerfile、运行与设计说明、`agent/conversation.json` 应进入同一提交。正式提测使用不含凭据的 HTTPS URL、平台接受的分支名及完整 40 位 SHA；推送代码本身不等于申请正式评测。

## 学习材料

只记录真实用户输入、必要题目上下文、Agent 输出、实际工具结果和明确标为计划的内容。学生贡献只能引用学生本人的发言；Agent 的规划或解释不能改写成学生理解。实验结果须直接包含在 `agent/conversation.json` 中，不能仅提供文件链接。后续记录需保留修正和复测的前后关系，禁止虚构问答、成绩、测试或复盘。

材料为 UTF-8 JSON 对象，最多 128 KiB，不含重复键、非标准数字或密钥。当前没有学生针对技术机制的解释、独立实验反馈或复盘，材料如实注明缺失。
