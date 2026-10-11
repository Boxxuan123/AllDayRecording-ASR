# 聊天事项收尾与手机接入验收（2026-10-11）

代码接入：完成。新增模型调用：0。实机闭环：NOT_RUN（`hdc list targets` 返回 Empty）。没有重新分析已完成的 7,678 条／103 批，没有创建生产提醒或发送消息。

## 实际基线与范围

- ASR 执行前 origin/master：`014c38d8d2d2e8046439c8ca27bd9b564d271f8f`。
- Phone 执行前 origin/master：`c7b6fa8b8dc7ffa60e0a38b2321e3816589314b0`。
- 在隔离工作区提交本轮修改；两个原工作区的既有修改通过三方合并保留，没有重置或提交它们。
- ASR 0.6.3、Phone 1.0.4 / 1000004 / build 5；共享契约 3.9.0、投影 6、Core 迁移 33、Phone 投影库 20。
- Mac 服务、Watch 业务代码和设备未修改。没有新增第二套事项状态或录音身份占位。

## 四类修补

1. `domain/chat_admission.py::evidence_relation` 比较原证据签名，区分增加进展与替换原文。实际 `accept_candidate` 接纳之后，完成／改期建议通过原分析准入链路保存到 source.conflict_json，并发布同一个 Event 的新修订；人工标题、归属、确认及处理状态保留。桌面和手机显示采用／拒绝入口；原事项接纳不代表这次变化已采用。替换证据禁止采用，可拒绝后核对来源。已接纳重复操作和旧响应重放不复活已完成／忽略事项。
2. `bootstrap/chat_followup_context.py::augment/verify_records` 从同 dataset、同会话的既有事项补齐有限 anchor、责任证据。验证稳定 ID、修订、冻结原文 SHA；身份读取字段单独比较。最多 15 个关联事项、18,000 字节额外证据，并继续使用现有完整请求预算。不可信或超预算关联明确标记。早批“我负责提交资料”、后批“资料已经发给你了”保持同一个 Event。
3. `domain/chat_followups.py::validate_candidate` 可读取 time_record_id；旧响应只在唯一可证明的表达证据下推断。稳定原 anchor 不变，相对时间用表达消息和明确来源时区解析。10 月 9 日建立、10 月 11 日“改到明天提交”得到 10 月 12 日；来源／时区不唯一则未知。
4. `bootstrap/chat_followup_review.py::candidates` 遍历保存作业，未处理旧候选可访问。响应／单项证据失败单独显示并禁接纳，有效项保留。`FollowupsView.vue` 分开加载失败、尚未加载与确实为空，进展与原接纳标记分别显示。

## 手机数据与操作闭环

`application/chat_followup_projection.py` 发布 `chat_followup`：同 Event ID、当前修订、标题、承担者、状态、时间、不确定性、历史进展未知、原话和引用／转发上下文，以及待确认变化。证据类型为 `chat_record`，没有伪造 session、utterance 或音频范围。

Phone 的投影库 19→20 新增可重建聊天投影；既有游标、离线 outbox、回执和 Calendar mapping 保留。`PhoneChatFollowup` 和任务页复用 `PhoneV3UseCases.queueReminder` / `chat.followup` 命令，支持确认当前、编辑、完成、忽略、重新开启、采用／拒绝变化。Windows 的 `apply_chat_operation` 在现有同步 UOW 的 savepoint 中执行同一事项操作，失败不会半写入；重复操作复用回执和 Event ID。回执到达但投影未到达时不提前删除离线意图。

首次提醒经 `ChatFollowupCommands.act(enable_reminder)`、`IntelligentReminderService` / `ChatReminderMixin.enable_chat_reminder` 创建原 Event 的 ReminderSchedule，录音来源字段为空且 source_type 明确为 chat_record。需要本人、当前未结束且用户确认仍需执行，并输入带 Z 或偏移的明确未来时间。只有日期／未知时间不会自动补时刻；历史事项默认保持进展未知。首次创建、改期、完成／取消与重试使用原 Calendar Kit 专用日历及稳定 identifier，只有日期的已采用改期或人工编辑将已有日历提醒标为 stale 并移除旧时刻；补充确认准确时间后恢复同一个 Event/ReminderSchedule 和稳定 Calendar identifier。旧录音任务继续使用原路径。

权限不足、写入失败、外部改动／删除显示原 Calendar 状态和重试／重新启用入口；日历写入不等同于已经实测系统通知。

## 实际验证

- 最终后端相关套件：244 passed、无跳过，包含真实两仓库契约收据。大批临时 SQLite 线程等待从 8 秒改为有界 30 秒，消除 Windows 测试误报；生产模型／作业预算未变。合入原工程修改后的当前工作区兼容回归另外 166 passed。
- Vue：4 项测试、vue-tsc、Vite 构建通过；合入原工作区后单独构建，保留原 UI 工程修改。
- Ruff 检查与格式检查通过。
- `tools/quality/check-chat-followup-loop.cjs` 使用真正 Phone SQLite/outbox 与 Python 临时数据库同步对端：实际接纳入口、保存响应重放、含已有离线命令／游标／Calendar 关联的实际 19→20 迁移和重复升级、首次 Calendar、明确时刻改期、日期改期暂停与补时刻恢复、完成、重新开启、取消、离线重启、重复回执、权限失败恢复、手机采用／拒绝后续变化及旧响应保护通过。模型入口一旦调用会失败；Calendar port 为 fake，不计实机通过。
- `check-calendar-reminders.cjs`、`check-product-reminder-loop.cjs` 通过，覆盖旧录音提醒、外部修改／删除、失败重试及创建回包丢失后的稳定对象恢复。
- 手机最终完整 ArkTS 编译、签名构建通过（51 tasks）；严格宿主 Hypium 170/170（Watch 68、Phone 102）通过。原测试旧投影常量改为共享契约常量，实际业务断言保留。
- Code Linter 当前 SDK 输出 8 个既有 disabled await-thenable 诊断，严格仓库门禁计为失败；均来自未修改的 Watch 诊断／传输和 Phone 原电脑／录音运行层。本轮聊天文件无诊断，没有为消除门禁而修改这些无关路径或放宽规则。

## Windows 正常启动核对

部署前使用 SQLite backup 保存一致性私有备份。工作台只对核实归属的既有进程发送正常关闭信号，没有强制终止正常采集器。重启后迁移 33，`/chat`、`/followups` 及实际候选 API 返回 200。277 个 Event、434 次 Event 操作、原聊天记录、三个作业／断点和人工状态指纹一致，提醒数量仍为 0。

业务表仅新增迁移记录、1 个已接纳聊天事项的 change_events 投影，并把原同步游标投影版本改为 6（ack sequence 保留）。身份 observation 仅刷新 observed_at，身份内容逐列一致；没有重新推理或生成新事项。已有配对继续使用，手机需升级本轮投影 6 版本再同步。

## 操作入口与实机 NOT_RUN 步骤

1. Windows 打开 `http://127.0.0.1:8765/followups`，在“分析候选与待处理变化”接纳目标事项；已接纳事项无需再次接纳。
2. 安装本轮 Phone HAP，沿用配对电脑。在 ASR 项目使用既有接收服务；本轮仅同步事项可运行 `allday-asr device receive --no-auto-process`，默认使用同一 `state/v3` 和既有配对身份，不触发自动录音／模型处理。当前设备接收服务未运行，实机连接也未执行。手机点击同步电脑，打开现有“任务”页的聊天事项卡片。确认、编辑、完成、忽略、重新开启及待确认变化都在卡片中。
3. 点击“启用提醒”，填写明确带时区偏移的未来时刻，点击“确认当前仍需执行并启用提醒”，授权日历权限，核对调度结果；不填写录音任务 ID。
4. 有设备后，用明确标记 `CHAT_PHONE_LOOP_TEST` 的单一合成事项完成一次：接纳→同步→启用未来提醒→查询专用日历仅一个对象→手机离线改期／重启／重试→电脑同一事项修订→后续完成建议采用→两端结束且日历对象移除。拒绝建议应保持人工状态。应用覆盖安装前按项目现有备份规则保护沙箱。
5. 实机未执行：Phone 覆盖安装、Calendar Kit 真实权限与事件、手机和 Windows 网络同步以及系统通知观察。没有连接设备，不等待或伪写通过。

## 架构复审说明

本轮跨 UI、Transport、Storage；现有 PhoneV3ViewModel、PhoneProjectionRepository 超过 500 行，按项目阈值已复审。新增纯聊天 DTO／映射／页面组件与 Windows 命令、投影、关联补证据分别拆分；事务、队列、回执和 Calendar 继续由原协调器拥有。没有为行数另建状态存储或同步实例。Core/Application 不导入 SQLite；SAVEPOINT 通过 UOW 端口实现。

所有缓存、私有原话、附件、凭据、完整数据库、签名材料和日志留在 Git 忽略的本地目录，未纳入提交。

当前运行目录仍包含原有未提交工程修改，构建身份保持 dirty；本轮远端提交来自干净基线的隔离工作区，不将混合工作区冒充发布镜像。
