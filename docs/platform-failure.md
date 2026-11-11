# 平台准入失败排查

记录始于 2026-11-09，更新于 2026-11-11，Asia/Shanghai。状态：**旧交付准入失败已确认；旧通信协议不兼容已复现并修复，尚未重新正式提测。**

用户提供平台失败记录后，通过已登录浏览器读取了结果详情及该页面实际接收的
记录数据。未提交新评测，没有消耗新的提测机会。

## 已确认

- 提测时间：2026-11-09 21:54，记录 ID `47247992`。
- 仓库：`curryxjh/seekdb`；分支：`minivectordb-round2`。
- 完整提交：`3ccc14effd6501ffb10d5e6aaa0ba2853032cb86`，与上一轮交付一致。
- 平台任务状态“执行成功”仅表示任务结束；结果为成功 0、失败 1。
- 失败的是“MySQL 正式用例”准入项；页面没有给出 29 个用例的逐项结果。
- 性能和 Agent Prompt 均显示“MySQL 正式用例未通过，不计本项”。
- 页面结果详情仅为“未通过；此项为准入条件，不计排名分”。
- 页面数据中的 `errorMessage` 为 `null`，没有构建/启动日志、首条失败 SQL、
  超时信息、资源限制诊断或实际/预期响应。

脱敏后的必要字段见 [记录数据](results/platform-admission-failure.json)。
未保存原始响应、截图或仓库认证信息；记录不含凭证及用户账号资料。

## 已建立的同一边界复现

官方在 2026-11-10 发布了 `STDIO_PROTOCOL.md` 和 `protocol_check/`。原样驱动对
旧交付的最小握手检查稳定失败：

```text
handshake: FAIL expected ready, got {'ready': True, 'protocol': 'minivectordb-local-v1'}
```

旧程序输出的是自定义 ready 标记并等待分号分隔的 SQL；官方协议要求严格的
`{"type":"ready"}`，之后逐行接收 JSONL execute/shutdown 请求，原样回传请求
`id`，错误使用对象形式，并在 shutdown 后返回响应再退出。这个差异足以解释旧
交付在 MySQL 准入阶段失败，且不涉及修改题包断言或绕过测试。

修复已将通信边界改为官方 JSONL，并把官方新增文件原样合入。原有公开
`mysqltest` 文件未改动。原样官方驱动验证结果：

- native：20/20 用例、95 条 SQL 通过；
- Linux amd64 Docker 镜像：20/20 用例、95 条 SQL 通过；
- 握手、请求 ID 精度/类型、严格 JSON、转义、错误后继续、ANN 字段、关闭回滚
  和重启边界测试通过；
- 96 MiB、无 swap、40000 行资源测试通过。

失败复现保留在 `results/protocol-before/result.json`，修复后的报告保留在
`results/protocol-docker-public/result.json`。这些是本地官方驱动结果，不能替代
平台隐藏 9 个用例或新的正式提测结果。

## 当前状态

原记录没有重新提测，也没有消耗新的正式机会。修复后的代码、Dockerfile、官方
驱动和验证材料需要以同一个新 commit 提交；正式平台准入、性能排名和 Agent
Prompt 排名仍待用户决定是否提测后确认。
