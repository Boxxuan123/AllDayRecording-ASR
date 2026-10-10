# Mac 查询性能修复跟踪

**2026-10-10：已修复并在 Windows 原确切范围重验通过。实际 service 1.0.1；用户提供修复 commit `b668bc48c3320c80f2ccbefbc8e90989618973de`（FROM_HANDOFF）。最新详细结果见 [Windows 复验报告](CHAT_QUERY_V1_RETEST_20261010.md)。**

Mac 交接确认原查询选账号/seq路径导致稀疏末页扫描；已增加会话/时间覆盖索引并先读候选seq再回读正文。这是 Mac 提供的实现说明，Windows 未读取其源码/EXPLAIN；Windows 独立实测证实原超时窗口已完整返回。

## 2026-10-09 首次待修记录（保留）

2026-10-09 的 Windows 真实跨机联调已鉴权成功：CHAT_DATA_CONTRACT_V1、service 1.0.0、schema 1。消息按 ID 和上下文可读且原文一致，问题集中在有范围的查询分页。

## 已复现

三个固定会话：QQ 两个 c2c、微信一个 group。每个范围使用记录自身的 platform/source_account_id/conversation_id；时间固定为一个已捕获锚点前 60 秒至后 3600 秒。`limit=2`：其中 QQ 一个会话和微信群的 GET /v1/messages 返回 503 QUERY_TIMEOUT，约 10.1–10.6 秒。另一个 QQ 会话第一页可在约 0.22 秒返回，但 has_more=true，尚未验收最后一页。

POST /v1/snapshots 对三个范围都成功，boundary_cursor 后的 GET /v1/changes 都成功（当次为空）。带 snapshot_id 的 GET /v1/messages 在相同两个会话仍 QUERY_TIMEOUT。三个锚点 POST /v1/records 约 0.03 秒且与原消息严格等值；context 约 0.05–1.36 秒且没有串会话。两平台的 2021 年前历史普通首屏各读到两条。

具体私有范围、锚点、snapshot 和响应计数留在 Windows `state/chat-acceptance/`，不公开账号、会话 ID 或原文。

## 修复与再验建议

请在 Mac 对上述真实范围检查实际查询计划，包括 scope 的 source_account_id/conversation_id/time 组合、固定捕获边界及下一页/末页的判定。**是否缺少组合索引、是否为了证明无更多结果扫描整个投影，是待核实推断**；Windows 没有读取 Mac 实现源码或任意 SQL 权限，不断言已经确认根因。

修复应维持契约的字面 keyword、change_seq 升序、稳定快照/游标、范围身份及首次修订语义，不改成按 sent_at 推进增量，不用静默截断或空结果掩盖超时。仅增加 Windows 等待时间不能修复 Mac 的 10 秒 SQL 限制。

在真实 366 万条左右的投影上再验：完整分页的小会话/重点群；0 条、1 条和稀疏结果的末页；中文短词 + 平台 + 采集账号 + 会话 + 发送者 + 时间的组合；snapshot 初始范围与 boundary_cursor 接续。每项应在契约上限内返回明确结果。

Mac 修复交接后，Windows 继续完整固定范围/真实问题验收。当前 Windows 本地成果已保留，状态 BLOCKED；不能把局部缓存或 fixture 当成全量历史查询已通过。
