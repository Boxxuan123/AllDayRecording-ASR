# 一次性本人待办闭环

2026-10-01：实现与隔离测试已完成，真机验收待完成。当前不能据此宣称可日用。

自动上传处理在 durable ASR 与已有本人 inference 后调用 `ReminderExtractionService.extract_product`，无需 Codex 登录或网络。只处理有效、可用 speech 且投影身份为 self 的明确第一人称任务。示例“我明天下午三点给老师发材料”以 utterance.start_at 和会话时区解析为绝对时间，延迟上传不会把日期再推后一天。

支持今天、今晚、明天、后天和明确年月日，带上午/下午等时段或明确 24 小时时间；支持数字/中文小时、分钟和半点。模糊小时、过期时间、身份不确定、否定、完成、取消、周期、条件、疑问及明显第三人执行语句均跳过。该解析器采用保守规则，会遗漏范围外表达。

复用 `generation_records`、`structured_change_proposals`、`reminder_candidates`、`event_operations`、`event_current_states`、`reminder_schedules` 和反馈表。原话、录音时间、身份、utterance/revision、时间表达、时区、绝对时间和提取版本保存于现有 provenance。没有新建 schema 或第二套任务模型，没有修改本人算法和学习策略。

候选经既有 review 快照同步到手机，显示动作、执行人、时间与原话，并提供来源录音入口。确认、修改后确认、忽略均进入既有 SQLite outbox，经 `/device/v3/sync` 的 `reminder.review` 操作和不可变回执落地。确认前不会调度系统通知。已确认任务通过 `reminder.task` 改期、取消或完成，以 event revision 检测冲突。

同源键使用 session、录音范围、动作和绝对时间 SHA-256，写入既有 `idempotency_records`，与 generation/proposal/candidate 同一事务。重复处理保留最初候选和人工决定；超过最近列表窗口的旧候选仍可直接寻址。手机回执等待指定 event revision 投影后清理 outbox，已消费 review tombstone 阻止过期快照复活候选。

手机复用 ReminderAgentCalendar；同 event group 保留一条系统请求。断网改期、取消、完成保存在 outbox，并在本地重载时应用；取消与完成撤销系统请求，改期替换旧时间。通知权限拒绝和调度失败显示于任务页，过期任务不会向后滚动。设备能力、签名、锁屏/后台实际触发和系统重启行为必须另做真机验收。

当前沿用 SDK 26 的 UTC `FIXED_TIME_ZONE` 请求；工程 compatible SDK 为 23，构建仍会提示该既有 API 的兼容警告。目标手机实际是否支持这项能力尚待连接验证，不能把主机 mock 的通过当成低版本兼容证明。

隔离 Python 用例：`python -m pytest tests/test_product_reminder_loop.py`。完整回归：`python -m pytest tests`。数据标记 `PRODUCT_REMINDER_E2E_TEST`，仅在临时数据库运行，不写生产、Blind、学习或人物画像。
