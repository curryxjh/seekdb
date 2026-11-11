# 本工作区约定

- 基于组织方 `2026-competition-round2` 开发 MiniVectorDB；本地开发分支按用户要求使用 `round2-dev`。
- `origin` 为 `https://github.com/curryxjh/seekdb.git`，`upstream` 为组织方仓库。不得覆盖目标仓库已有分支或将令牌写入文件、Git remote、提交或日志。
- 正式分支名与开发分支名的差异见 README；不能把分支改名视为平台已接受新名称。
- 当前驱动实际要求 stdin/stdout JSONL；先读 `STDIO_PROTOCOL.md` 与驱动，再实现通信。不要根据背景文案把它改成纯 SQL 输入。
- 存储、查询执行、向量索引及事务核心自行实现，禁止用现成数据库或向量索引库代执行。支持超过内存预算的数据；所有运行数据及恢复所需文件位于 `/data`。
- 维护 `agent/conversation.json`，随代码、Dockerfile 纳入同一次提交。仅追加真实交互和实际验证；区分 student、agent、tool，不得代写学生发言、捏造理解或实验结果，未执行内容写为计划。
- 学习记录须为 UTF-8、合法 JSON 对象、不超过 128 KiB；不得含重复键、NaN、Infinity 或凭据。关键证据放在字符串字段中，便于 JSON pointer 定位。
- 测试通过需有实际结果；骨架握手通过不能表述为 SQL 或正式准入通过。正式评测每日次数有限，不自动发起正式提测。
