# 一次性本人待办闭环

2026-10-02：实现、隔离测试和手机审核/任务操作已验证；真机系统发布返回 1700002，实际通知尚未通过。当前不能据此宣称可日用。

自动上传处理在 durable ASR 与已有本人 inference 后调用 `ReminderExtractionService.extract_product`，无需 Codex 登录或网络。只处理有效、可用 speech 且投影身份为 self 的明确第一人称任务。示例“我明天下午三点给老师发材料”以 utterance.start_at 和会话时区解析为绝对时间，延迟上传不会把日期再推后一天。

支持今天、今晚、明天、后天和明确年月日，带上午/下午等时段或明确 24 小时时间；支持数字/中文小时、分钟和半点。模糊小时、过期时间、身份不确定、否定、完成、取消、周期、条件、疑问及明显第三人执行语句均跳过。该解析器采用保守规则，会遗漏范围外表达。

复用 `generation_records`、`structured_change_proposals`、`reminder_candidates`、`event_operations`、`event_current_states`、`reminder_schedules` 和反馈表。原话、录音时间、身份、utterance/revision、时间表达、时区、绝对时间和提取版本保存于现有 provenance。没有新建 schema 或第二套任务模型，没有修改本人算法和学习策略。

候选经既有 review 快照同步到手机，显示动作、执行人、时间与原话，并提供来源录音入口。确认、修改后确认、忽略均进入既有 SQLite outbox，经 `/device/v3/sync` 的 `reminder.review` 操作和不可变回执落地。确认前不会调度系统通知。已确认任务通过 `reminder.task` 改期、取消或完成，以 event revision 检测冲突。

同源键使用 session、录音范围、动作和绝对时间 SHA-256，写入既有 `idempotency_records`，与 generation/proposal/candidate 同一事务。重复处理保留最初候选和人工决定；超过最近列表窗口的旧候选仍可直接寻址。手机回执等待指定 event revision 投影后清理 outbox，已消费 review tombstone 阻止过期快照复活候选。

手机复用 ReminderAgentCalendar；同 event group 保留一条系统请求。断网改期、取消、完成保存在 outbox，并在本地重载时应用；取消与完成撤销系统请求，改期替换旧时间。通知权限拒绝和调度失败显示于任务页，过期任务不会向后滚动。设备能力、签名、锁屏/后台实际触发和系统重启行为必须另做真机验收。

真机发现新建候选 expected_revision=0 被错误投影为 source_revision，手机严格 DTO 将其过滤。审核投影现将无现有事件修订的创建候选序列化为 null，保留正修订与手机校验。

Calendar 原生构造先按本地时间校验，之后才读取时区模式；传 UTC 日期字段会在 UTC+8 设备被判为过期。现在从持久化的绝对时刻转换为手机本地日期字段，使用默认 Calendar 请求，取消 API 26 fixedTimeZone 依赖。跨时区后需应用重新调度，未证明旅行期间应用不运行时仍按原绝对时刻触发。

真机当前有效系统提醒为 0，publishReminder 仍返回 1700002。当前调试签名 Profile 的 allowed-acls 为空；需核对 AppGallery Connect 的代理提醒开放能力及更新后的签名 Profile。没有以常驻轮询或前台即时通知绕过系统限制。候选和任务状态成功不等于系统通知已经安排。

隔离 Python 用例：`python -m pytest tests/test_product_reminder_loop.py`。完整回归：`python -m pytest tests`。数据标记 `PRODUCT_REMINDER_E2E_TEST`，仅在临时数据库运行，不写生产、Blind、学习或人物画像。
