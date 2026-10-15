# 工程重构执行与验收记录

日期：2026-10-07。起始基线：`87532be`；交付形态：当前工作区代码、测试及文档，未创建提交。依据 `PROJECT_ENGINEERING_REFACTOR_GUIDE.md`，保留原审计及旧整改报告。

当前状态：第 11 节 FUP-01B 已完成，FUP-01 / ENG-01 恢复 DONE，其余任务保持 DONE。最终后端 **1010 passed / 1 skipped，184.50 秒**；Ruff、Phone 契约、前端测试、类型检查及构建均退出 0，证据为 `outputs/engineering-fup01b-final-r2/`。关联套件 202 项、最终提醒/TLS 定向检查 138 项通过。Phone 仓库源码未改，本轮未重跑 Phone 构建、170 项主机测试或独立 SQLite 消费脚本；历史通过记录保留。生产历史处理、真实模型、GPU 和实机验证仍为 NOT_RUN。

## 任务记录

下表行为描述及其测试结果保留首轮实施历史，状态列反映最新复核状态；第二轮关闭证据保留，新增 P2 收尾见下方 FUP-01B 记录。下列路径均相对本仓库；命令与证据章节保留统一日志和可重复命令。相关旧测试保留，新增测试使用独立合成数据，不依赖 outputs 中的历史数据。

| 任务 | 状态 | 最终行为、实际入口及行为验证 |
|---|---|---|
| ENG-01 | DONE（FUP-01B 已关闭） | 新 `application/generation_inputs.py` 捕获版本 1 的实际输入快照。`event_extraction.py`、`reminder_extraction.py` 在读取输入时捕获全部使用的 utterance/身份/提醒上下文；`knowledge_queries.py` 在提交事务校验，`knowledge_generation.py` 在接受并发布的同一 UOW 再校验。`insight_generation.py` 比较完整实际上下文。`reminder_commands.py` 的人工修改保留原快照。真实 correction 交错、两个接受 actor、失效来源、上下文变化、无关对象、旧 pending、重复接受均有断言；原确认任务/Calendar 保护测试保留。 |
| ENG-02 | DONE | `adapters/codex/model_execution_runner.py` 的 v2 execution 由 task、entity、规范化输入、prompt 前缀、模型、协议、effort 和显式再生成 ID 定义。首次调用前持久化固定 prompt/now_utc；重试、重启、缓存命中使用相同 ID 和预算。模型结果、job/receipt、generation scope/provenance携带 execution ID。event/reminder/insight 三个 adapter 使用新身份。测试覆盖 30 分钟后新 revision、时间抖动、重启不回满、显式新执行、缓存不重复远程启动；原 runner 限次/token/call/并发用例保留。 |
| ENG-03 | DONE（FUP-02 已关闭） | `adapters/transfer/automation_state.py` 使用私有 SQLite 状态和 retry outbox。请求接受、状态修改在事务内；consume 改为可重放读取；持久 queued 与 scheduled ACK 同一事务，随后才内存入队。`automation_retry.py` 恢复 retry_requested、queued/running 和到期 retry；active 会话不 ACK。测试覆盖消费后重启、持久调度前后、执行后、active 去重、16 个并发入口共用 request ID、旧无 request 文件、磁盘事务失败。既有设备/自动流程测试验证副作用幂等。 |
| ENG-04 | DONE | 新 `application/failure_diagnostics.py` 提供错误类别与脱敏的 chained stack frame 记录；不记录异常消息或源码字面量。`desktop_server.py` 关联响应 request_id；`daily_events.py`、`daily_automation.py`、`automation*.py`、`processing_worker.py` 记录适用稳定 ID。provider 结构拒绝与非法输入区分，budget terminal/取消不自动重试。桌面 `/api/v3/worker-health` 暴露 Daily/采样健康；receiver 有 workflow health。真实 HTTP 注入测试关联响应/日志，并断言敏感占位文本不出现。 |
| ENG-05 | DONE（FUP-03 已关闭） | `bootstrap/core.py` 始终组装 Daily coordinator；sync 回调只 notify。`daily_automation.py` 的本地维护和远程执行共用 OS owner 锁，每轮最多 64 dirty 行、14 个唯一日期（包含今天/昨天）。本地失败保留 dirty；ACK 只消费读到的范围，处理期间新写不丢。模型关闭仍本地维护；删除来源的已发布旧日仍进入 inventory，并标记 stale/保留内容。新远程历史工作仅显式 backfill；近期和已准入任务可恢复。真实 compose→sync 的 7/90/365 天 fixture 包含 utterance、人物、已确认任务，断言同步不 refresh，重启无 notify 仍只维护受影响日/近期日。 |
| ENG-06 | DONE | `daily_source_repository.py` 批量 audio range、身份引用、人物名和 task evidence；`daily_event_inputs.py` 不拉整份 session detail。1→101 条 utterance 的 SELECT 数不逐句增长，固定 fixture 的输入 hash 与独立旧基线相同。`daily_inventory.py` 发出 reason_code；`daily_generation_queue.py` 不以 reason 展示文案决定状态。原 lease/次数/backfill 测试以及新增文案独立、非法 retry、旧 owner finish、上限不回退测试通过。产品时区继续为 Asia/Singapore；日期边界沿用 UTC→Singapore 的现有规则。 |
| ENG-07 | DONE | 新 `adapters/models/isolated_native.py` 为生产 native factory 提供独占 Windows Job 子进程。模型加载、音频准备、双 ASR、diarization、产物写入与 backend close 均受 stage/progress deadline；lease 定时续租不算 progress。只终止本 Job；等待成员完成句柄和 ActiveProcesses 清零。`ports/processing.py` 可返回已在子进程内容寻址存储的 immutable artifact，`processing_service.py` 校验身份后只发布元数据。`processing_worker.py` close 阻止新 claim 和返回后的发布，shutdown 保持 retryable。六种挂起阶段、自有后代清理、长任务进展、取消/关闭、伪造产物、关闭后迟到结果及重启恢复均经合成测试；真实 GPU 见 NOT_RUN。 |
| ENG-08 | DONE | `annotation_sample_repository.py` 的 120 秒 lease 续租有 token/generation/未过期约束；`annotation_samples.py` 从 snapshot/planning 前开始每 30 秒续租，最多一小时，close/失租停止，发布再次验证 revision/model。fake clock + 慢 embedding 模拟 200 秒正常任务，验证第二 worker 不重复领取；旧 owner 续租/完成、模型/来源变化、重启、耗尽、close 后领取测试保留。health 提供 queued/running/failed、最老等待、last success/error；连续基础设施错误三次停止，close 超时可见。年龄与入队一次写入，保留批量标注只更新队列一次的原断言。 |
| ENG-09 | DONE | 新公开 `application/evidence_identity.py` 供 event/reminder/daily 使用，保留审核不确定性及各用途限制。`domain/dataset_roles.py` 是纯判断，SQL 在新 `adapters/sqlite/dataset_role_repository.py`。`recording_repositories.py`/recording ports 提供 manifest 业务能力；ingest/completion 不穿透 connection。新 `ports/daily_maintenance.py` 明确 inventory/queue/dirty/service/ownership 依赖；coordinator 使用公开 execution observation，不读取 analyzer 私有字段。路由沿用现有 GET/POST 能力 dispatcher，只增加必要入口/错误映射。AST gate 查禁止 import 与 repository SQL 穿透，区分模型 execute。原 policy CAS、purity、设备审核测试通过；保留 cohesive repository mixin。 |
| ENG-10 | DONE | 新 `v3/core/reviewInbox.ts` 分离主列表与辅助候选；`ReviewsView.vue` 保留列表，显示局部错误/重试，缺候选操作禁用，分页保护晚到请求。`query.ts` 保留共享 in-flight/subscriber/version 保护，增加 30 秒 TTL 清理、128 项容量及 active key 生命周期；证据修改失效概览、录音、三种 session 视图、人物、审核/候选、Daily/关系缓存。`SessionDetailView.vue` 单条校正与批量分类保存均使用相同失效入口。真实 Vue 挂载测试覆盖失败/重试/按钮、共用请求取消、晚到、TTL/淘汰、编辑后返回及无关录音缓存保留。 |
| ENG-11 | DONE | `knowledge_queries.py` 的重算账本明确 execution_mode=manual、action_required=regenerate_from_current_sources。`event_extraction.py` 不将 stale event 永久算作 known；同一稳定 identity 通过 UPDATE + revision CAS 再生成，旧 provenance 保留，重复提取不产生等价 active 事件。桌面 semantic-event generation 路由及原 CLI 可达；显式 execution_id 用于新执行。未新增通用自动消费者，模型关闭仍可查询待办；确认任务意图不会自动重算。 |
| ENG-12 | DONE | `tools/validate_engineering_refactor.py` 默认执行全部六项检查，独立 stdout/stderr/退出码和 pytest XML，无静默 skip；`tools/reproduce_engineering_baseline.py` 归档旧代码到独立目录并复制当前回归测试。旧基线 8 个正式行为案例均 assertion failure，无 collection/import error。schema 30 空库、29 升级、重复启动、实际迁移 SQL 异常回滚、checksum 及早期标注回填升级均覆盖。最终完整矩阵六项通过，源码/测试校验摘要验证测试后无改动。 |

## 独立复核与第二轮补修（2026-10-07，历史验收）

依据：[修改指导第 10 节](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_GUIDE.md#10-独立复核后的补修指导第二轮)。保留[独立复核记录](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_REVIEW.md)的发现事实。起止 HEAD 均为 `87532be`，交付当前未提交工作区，未删除原日志、测试或历史 manifest。

| 补修 | 原任务 | 状态 | 最终行为 |
|---|---|---|---|
| FUP-01 | ENG-01 | DONE（FUP-01B 已关闭） | 单条规则实际证据快照；独立候选正常确认；模型全上下文和同目标 CAS 保留；显式可追溯恢复 |
| FUP-02 | ENG-03 | DONE | 原 JSON 一次性事务导入；跨进程请求/调度 CAS；完整执行后不重复排队；只合并有证明的未领取冗余项 |
| FUP-03 | ENG-05 | DONE | 失效和同步日志同 UOW；同内容 revision 的 stale 更新；Phone 序列保护；明确日期的有界历史补发 |

### FUP-01B 关闭记录（2026-10-07）

执行依据为指导第 11 节。状态 **DONE**；FUP-01 / ENG-01 恢复 DONE。起止 HEAD 为 `87532be`，交付当前未提交工作区。新增/修改 12 个源码及测试文件，完整当前源码 manifest 为 99 项（96 ASR、3 项此前已修改的 Phone 文件）；本轮 Phone 文件无变化。未追加或改写迁移，主库、sidecar、Phone schema 与 wire contract 保持原版本。

发现时证据保留：`outputs/engineering-followup-independent-review/` 的 950 passed / 1 skipped、169.29 秒，六项退出 0；`outputs/engineering-followup-phone-independent-review/` 的 SQLite 消费通过；`engineering-followup-final-r4/source-manifest.json` 的 93 项为收尾前基线。Phone 构建和 170 项主机测试为此前结果。`engineering-followup-review-model-edit/result.json` 的独立复现已转为正式 B01 测试，以上历史日志及 manifest 均未覆盖。

**正式失败证据及实现**：B01 在修复前通过真实 `modify → submit → accept` 路径抛出预期的 GenerationSourceConflict（1 failed / 64 deselected，0.59 秒）；修复后通过。新的 `reminder_edit_sources.py` 只沿持久化 revises_candidate_id → candidate → proposal → generation 解析原批次，校验 pending 状态、成功 generation、会话、证据、原快照及不可编辑字段。新人工 generation/operation 保留自己的身份，禁止调用者填入任意批次 ID、缺失关系、跨会话、错配或循环关联。所有编辑保持原始 input_snapshot，不以当前 revision 更新旧输出。

前置修改校验、替代 generation 提交 UOW、最终接受 UOW 共用此规则；最终发布新事件/日程/同步投影、替代候选 confirmed、原候选 modified 与 MODIFY feedback 在同一事务完成。严格保留原同批 CREATE 输出证明及目标 revision CAS；额外检查持久化已接受输出不得因取消/完成而从 active 集合消失后获得豁免。外部改期、取消、完成、额外任务、来源 correction、身份和会话变化均拒绝。

同一规范化编辑由服务端确定 product_source_key。两阶段提交后若发布失败，允许保留可追溯的 pending 替代候选，不标记原候选 modified，也无新增日程/同步副作用；重启重试复用同一替代 generation/candidate。并发确认、相同/不同编辑最多一个生效决定，终态原候选不再新建任务。旧 pending 编辑只有能证明既有来源关联时才可接受；无证明时拒绝，不补写当前快照，不自动恢复生产数据。

额外发现通用 `/proposals/{id}/accept` 可绕过提醒发布回调，留下语义事件与 modified 原候选而没有提醒日程。先增加真实旁路失败测试（1 failed / 120 deselected，0.60 秒），再要求关联编辑必须经 reminder review 回调接受；拒绝发生在写入之前，同一 pending 替代候选可随后经正常 reminder.confirm 成功。

ASR 的 Phone reminder.review edit 持久保存同一 revises 关联，并在同步写事务共用校验与原候选收尾规则；proposal 局部 resolved_inputs 只按自身证据比较 revision，而完整模型上下文仍由原快照验证。Phone receipt 重放与桌面/Phone 等价场景通过，本轮无需改 Phone 仓库或协议。

| 编号 | 实际测试函数（tests/test_product_reminder_loop.py） | 验证结果 |
|---|---|---|
| B01 | test_fup01b_model_batch_modify_after_sibling_confirm | 修改后两个日程、原 B modified、替代 confirmed |
| B02 | test_fup01b_modify_order_restart_and_valid_fields；test_fup01b_invalid_edit_fields_do_not_create_attempt；test_fup01b_unchanged_edit_fields_only_supersede_original_candidate | 反序、真实关闭/重开数据库、合法时间/location/标题及未变字段成功；非法字段拒绝 |
| B03 | test_fup01b_actual_context_change_rejects_edit | 7 种真实上下文变化分别经桌面/Phone 拒绝；保留 A 的用户决定 |
| B04 | test_fup01b_transaction_gap_rejects_stale_replacement；test_fup01b_publish_failure_restart_reuses_unaccepted_attempt；test_fup01b_bare_knowledge_accept_cannot_half_publish_edit | 两窗口 × correction/原候选确认；原 modified 后 feedback 写入失败全部回滚；通用接受旁路无写入 |
| B05 | test_fup01b_concurrent_decisions_publish_at_most_once；上述 publish_failure_restart 测试；test_fup01b_valid_model_edit_has_equivalent_phone_and_desktop_result | Barrier 并发 confirm/edit、相同/不同编辑最多一次；持久重启复用、Phone receipt 重放无重复副作用 |
| B06 | test_fup01b_untrusted_edit_link_cannot_grant_batch_exemption；test_fup01b_final_accept_rechecks_persisted_relationship；test_fup01b_sqlite_rejects_candidate_generation_rebinding | 11 种提交伪造、6 种最终接受前关联变更拒绝；断言注入回调实际到达；SQLite 不可变关联触发器仍拒绝重绑定 |
| B07 | test_fup01b_legacy_pending_edit_requires_persisted_source_link；test_fup01_model_batch_outputs_require_persisted_batch_proof；test_fup01_same_model_target_conflict_keeps_revision_cas；test_fup01_modify_independent_candidate_uses_same_rule_dispatcher | 可信/不可信旧 pending 链区分；模型直接确认、产品规则编辑、同目标 CAS、原旧格式/未知版本和终态保护保留 |
| B08 | test_fup01b_valid_model_edit_has_equivalent_phone_and_desktop_result；test_fup01b_actual_context_change_rejects_edit；test_unexpected_storage_failure_rolls_back_task_and_receipt；device sync 套件 | 相同有效输入产生相同业务结果；真实冲突均拒绝；task/receipt 原子回滚与同步重放保持 |

正式执行命令（PowerShell，仓库根目录；完整六项命令、cwd、退出码和耗时见最终 results.json）：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_product_reminder_loop.py -k fup01b_model_batch_modify_after_sibling_confirm --basetemp outputs/engineering-fup01b-before/pytest-temp --junitxml outputs/engineering-fup01b-before/pytest.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_product_reminder_loop.py tests/test_v33_intelligent_reminders.py tests/test_v32_three_layer_knowledge.py tests/test_v32_semantic_events.py tests/test_v3_device_sync.py tests/test_engineering_refactor.py --basetemp outputs/engineering-fup01b-associated-r3 --junitxml outputs/engineering-fup01b-associated-r3.xml
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_transfer.py tests/test_product_reminder_loop.py --basetemp outputs/engineering-fup01b-final-fixes --junitxml outputs/engineering-fup01b-final-fixes.xml
.\.venv\Scripts\python.exe -X utf8 tools/validate_engineering_refactor.py --phone C:/Users/32673/DevEcoStudioProjects/AllDayRecording --output outputs/engineering-fup01b-final-r2
```

| 证据（均在 outputs，旧文件保留） | 实际结果 / 退出码 | 解释 |
|---|---|---|
| engineering-fup01b-before/pytest.log、pytest.xml | 1 failed / 64 deselected，0.59 秒 / 1 | B01 修复前预期业务失败；不是导入或收集错误 |
| engineering-fup01b-initial.log、.xml | 65 passed，10.72 秒 / 0 | 初始链路修复 |
| engineering-fup01b-matrix-r1.log、.xml | 6 failed / 40 passed / 64 deselected，10.04 秒 / 1 | 发现取消/完成遗漏和 Phone 全批 resolved_inputs 错配；另有 fixture 导入/唯一性错误 |
| engineering-fup01b-matrix-r2.log、.xml | 110 passed，21.30 秒 / 0 | 上述修复通过 |
| engineering-fup01b-associated-r1.log、.xml | 0 tests / 4 | 命令误用不存在的 test_semantic_events.py，未作为行为证据；改为真实 test_v32_semantic_events.py |
| engineering-fup01b-associated-r2.log、.xml | 1 failed / 200 passed，75.39 秒 / 1 | fixture 误改不可变 candidate generation，SQLite 正确拒绝；改为专门保护测试及可到达的关联变更 |
| engineering-fup01b-matrix-r3.log、.xml | 120 passed，23.93 秒 / 0 | 关联/跨会话 fixture 与回调到达断言完善 |
| engineering-fup01b-associated-r3.log、.xml | 202 passed，84.27 秒 / 0 | 产品/模型提醒、knowledge、语义事件、device sync、工程回归通过 |
| engineering-fup01b-final-r1/ | pytest 1 failed / 1005 passed / 1 skipped，195.92 秒 / 1；其余五项 / 0 | 仅 TLS PEM publication 发生 Windows WinError 5；保留该次 manifest 和失败日志 |
| engineering-fup01b-bare-before.log、.xml | 1 failed / 120 deselected，0.60 秒 / 1 | 通用 proposal accept 半完成旁路的正式失败证据 |
| engineering-fup01b-final-fixes.log、.xml | 138 passed，25.36 秒 / 0 | 最终提醒 121 项及 TLS 17 项通过 |
| engineering-fup01b-final-r2/ | **1010 passed / 1 skipped，184.50 秒 / 0** | 最终完整 pytest；脚本阶段墙钟 185.891 秒 |
| 最终 r2 的 ruff、contracts、frontend-tests、frontend-types、frontend-build | **全部 / 0** | 六项完整矩阵通过；每项 stdout/stderr 和实际命令保留 |

最终唯一 skipped 为 `tests/test_enrollment_purity_gate.py::test_private_historical_event_regression`，原因是既有 private local integration opt-in；未启用私有历史集成数据。

完整矩阵触发的 TLS 临时文件锁修复：`interfaces/transfer/tls.py` 的 PEM publication 复用既有 `file_lock.replace_atomic`，只对 Windows 错误 5/32 作两秒有界重试，保留 fsync、私有权限和临时文件清理。3 个新增测试覆盖实际 `_atomic_write` 的临时 5/32 锁以及持续失败（模拟时钟，保留旧 PEM 并清理临时文件）；原 Wi-Fi 地址改变后 CA 稳定测试及整个 transfer 套件通过，未改变配对或密码协议。未扩大 ignore、移除断言或放宽来源规则。

`engineering-fup01b-final-r2/source-manifest.json` 的 99 项在最终验收后再次逐项 SHA-256 一致；`fup01b-changes.json` 列出本轮 12 个源码/测试改动。3 项 Phone 哈希与旧 r4 manifest 一致。`delivery-manifest.json` 另记录当前四份文档、最终检查及关键失败/通过证据哈希；历史 source/delivery manifest 保留为历史快照。

NOT_RUN：本轮未运行 Phone debug 构建、170 项主机测试、独立 Phone SQLite 消费脚本或 OS Calendar 实机；ASR Phone 审核、receipt、device sync 和跨库 contract 已实际运行。未运行真实模型、GPU 长录音、Phone/Watch 实机，也未处理生产历史候选或批量恢复数据。恢复能力和明确拒绝规则已提供，不能据此宣称历史数据已修复。测试使用确定性 fake model 与独立合成 SQLite，不依赖生产 outputs。当前没有剩余必需代码/验收步骤。

### 未修复行为的正式失败证据

独立复跑基线 `outputs/engineering-independent-review/results.json` 六项退出 0，895 passed / 1 skipped，158.72 秒。新正式测试在补修前运行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_product_reminder_loop.py tests/test_engineering_refactor.py tests/test_daily_event_layer.py -k 'fup01_independent_product_candidates_both_confirm_through_sync or fup02_migrated_json_does_not_duplicate_new_retry or fup03_local_invalidity_publishes_stale_sync_projection' --basetemp outputs/engineering-followup-before-r2/pytest-temp --junitxml outputs/engineering-followup-before-r2/pytest.xml
```

结果 **3 failed / 1 passed / 83 deselected，退出 1**：desktop 第二候选 conflict、一次 retry 得到两条请求、失效后的同步日志没有第二条；均为正式行为断言，无 collection/import error。Phone 同场景当时通过，原因是 Phone review 绕过了 full-context 校验，本轮一并收口。证据 `outputs/engineering-followup-before-r2/pytest.log`、`pytest.xml`。更早首次命令的临时目录错误日志 `engineering-followup-before.log` 保留，不作为缺陷证明。

### FUP-01：来源快照、模型批次和历史恢复

`generation_inputs.py` 新增 v2/product-reminder，只保存规则用到的 ID/revision/text/status/self 身份、可用语音、录制起止、会话可用性和时区；在提取的同一只读事务捕获。`reminder_extraction.py` 不再将无关 utterance/提醒加入产品快照。提交、接受、修改共用版本分派器；未知版本/kind 拒绝。`knowledge_queries.py`、`knowledge_generation.py` 以及 `reminder_phone_operations.py` 在发布 UOW 校验。真实来源变化仍冲突，已确认意图保留。

模型仍验证 v1/reminder 完整输入。仅允许同 generation 产生且有持久 accepted proposal、candidate、未修改 CREATE operation 和对应当前事件/日程证明的新增上下文；外部改期、额外事件、既有上下文变更继续冲突，同目标 expected_revision CAS 不放宽。

`reminder_product_recovery.py` 只读识别有可信旧规则证据的 pending 或特定上下文误判 conflict。`reminder_commands.py` 在新候选提交事务重新核对资格，创建关联尝试、保留旧 generation/决定、记录 audit；旧 pending 明确退为 superseded conflict，原 conflict 原样保留。确定的新 source key 和旧 key 的 recovery redirect 使重复恢复、再提取解析同一替代候选。证据不足、实际来源改变和 confirmed/modified/ignored/completed/cancelled 不恢复。

入口：`GET /api/v3/reminder-candidates/{candidate_id}/product-recovery` 只读检查；带现有认证的 `POST` 同地址、空 JSON body，创建待确认替代候选，不自动确认。应用层 `inspect_product_recovery` / `recover_product(actor)`。生产候选恢复 **NOT_RUN**，仅在合成库实际执行验证。

| 指导案例 | 实际正式测试（tests/test_product_reminder_loop.py） |
|---|---|
| F01-A | test_fup01_independent_product_candidates_both_confirm_through_sync（desktop、Phone） |
| F01-B/C | test_fup01_independent_confirmation_order_and_restart；test_fup01_unused_source_change_and_single_candidate_replay |
| F01-D | test_fup01_actual_rule_source_changes_cannot_publish（文本/revision/身份/语句失效/会话删除/时区/身份待复核，两个入口）；test_fup01_unknown_snapshot_format_cannot_publish |
| F01-E | test_fup01_unused_source_change_and_single_candidate_replay；独立两候选及 modify 测试 |
| F01-F | test_fup01_model_batch_outputs_require_persisted_batch_proof；test_fup01_same_model_target_conflict_keeps_revision_cas |
| F01-G | test_fup01_legacy_explicit_recovery_preserves_history_and_is_idempotent（pending/conflict/untrusted/ignored/source_changed） |
| F01-H | test_candidate_edit_ignore_and_no_reappearance、test_ignore_survives_reprocessing_and_restart、test_reschedule_terminal_and_stale_revision、test_fup01_modify_independent_candidate_uses_same_rule_dispatcher；原确认意图与 Phone/Calendar 测试保留 |

### FUP-02：导入、领取和已存双请求

`automation_state.py` 定义 sidecar schema 3（主库仍 30），原 status/request JSON 与 `legacy_imports` 每来源标记同事务导入；不依据迁移后的 live 状态生成 legacy ID。request-only 孤立文件可恢复，重复初始化跳过已导入来源；SQLite 状态不会被保留 JSON 覆盖。`retry_requests` 增加来源和 queued 状态快照，`retry_request_audit` 记录显式修复。

`automation_retry.py` 调度前由 SQLite writer CAS 领取 pending request，queued/scheduled ACK/manual_retry_count 同事务后才内存入队。active 会话请求等待；queued 崩溃后恢复保持既有发布幂等。实际 poll→合成处理完成→再 poll 验证旧请求不再排队/count 不增加；随后新合法 retry 使用新 ID 正常执行。

只读报告展示 request ID、origin、status、scheduled 快照、workflow 关联及重复证明。仅原始导入证据表明没有旧 retry、legacy ID 精确由本次 UUID requested_at 派生且两项均未领取时可合并；保留所有行，将冗余项 superseded 并审计。不能证明、已有调度或合法旧 request 文件来源不合并。命令 `engineering-recovery retry-report` / `retry-coalesce`；生产双请求处理 **NOT_RUN**。

| 指导案例 | 实际正式测试（tests/test_engineering_refactor.py） |
|---|---|
| F02-A/B | test_fup02_migrated_json_does_not_duplicate_new_retry |
| F02-C | test_fup02_cross_process_initialize_request_and_schedule_are_unique（4 个 spawn 进程、8 次初始化/请求及并发调度）；test_concurrent_retry_entry_points_share_one_durable_request |
| F02-D | test_legacy_retry_requested_without_file_is_recovered；test_fup02_orphan_legacy_request_and_no_reactivation（有/无 state） |
| F02-E | test_fup02_complete_poll_execution_and_next_legitimate_retry |
| F02-F | test_fup02_import_marker_and_request_roll_back_together；test_corrupt_state_has_diagnostic_and_request_transaction_rolls_back |
| F02-G | test_fup02_repair_only_proven_unclaimed_legacy_duplicate（可证明/合法两次请求） |

原 consume/restart、queued 前后崩溃、active 等待、设备发布幂等测试继续通过。部署前停止并同时升级 receiver/desktop，避免旧 JSON writer 与 sidecar 权威混用。

### FUP-03：同步、历史遗漏及实际 Phone 消费

`daily_sync_projection.py` 共享构造限定 wire 字段，裁掉 objective.events。正常生成和 `daily_events.mark_daily_stale` 使用同一发布器；后者在 writer UOW 比较计划 revision、写 invalidation、读取有效状态并与最新日志 payload 比较后追加。失败全部回滚，coordinator 保留 dirty；已 invalidated 但日志漏发仍补发，相同 stale 不重复。内容/provenance/budget 不变，内容 revision 不虚增，sequence 推进；历史 bootstrap payload/watermark 不改写。

`daily_sync_differences` / `repair_daily_sync` 按明确 Singapore 日期检查最多 14 天；显式修复再次检查当前摘要，仅补发遗漏 stale 并审计数量，重跑为零。即使旧 dirty 已 ACK 也可覆盖；普通启动不全历史扫描、不发远程调用。CLI 为 `daily-sync-report` / `daily-sync-repair`。生产历史摘要补发 **NOT_RUN**。

Phone `PhoneSyncProjection.ets` 解析 cursor position；`PhoneProjectionRepository.ets` 对 daily_summary 使用持久 sequence 拒绝旧/重复变化，接受同 revision 的新状态，cursor 不倒退；显式 rebuild 在事务内重置 cursor。schema 19 / wire 3.8.0 / projection 5 均未变。

| 指导案例 | 实际正式测试 |
|---|---|
| F03-A/B/D | tests/test_daily_event_layer.py：test_fup03_local_invalidity_publishes_stale_sync_projection；test_fup03_real_correction_coordinator_incremental_and_bootstrap；原删除来源真实入口测试 |
| F03-C | test_fup03_bootstrap_watermark_retains_old_page_then_incremental_converges |
| F03-E | test_fup03_append_failure_rolls_back_invalidity_and_keeps_dirty_for_restart |
| F03-F/H | test_fup03_content_revision_cas_and_bounded_historical_projection_repair |
| F03-G | tools/check_phone_daily_sync.py + Phone tools/quality/check-daily-sync-stale.cjs：真实 compose/correction/coordinator/sync 响应 → 实际 PhoneRepository/PhoneV3ProjectionQueries/SQLite，覆盖同 revision stale、旧/重复 active、重启、bootstrap、写 row 后 cursor 失败的整体回滚 |

Phone 消费测试使用 Node 24.19.0 的真实 SQLite，OS adapter mock，**HOST_NOT_DEVICE**；结果 `outputs/engineering-followup-phone-consumer-r2/results.json` 退出 0，server-responses.json 和 stdout/stderr 同目录。

### 命令、关联结果和完整验收

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_product_reminder_loop.py
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_engineering_refactor.py -k 'retry or fup02'
.\.venv\Scripts\python.exe -X utf8 -m pytest -q tests/test_daily_event_layer.py tests/test_daily_automation.py
.\.venv\Scripts\python.exe -X utf8 tools/check_phone_daily_sync.py --phone C:/Users/32673/DevEcoStudioProjects/AllDayRecording --node C:/Users/32673/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe --output outputs/engineering-followup-phone-consumer-r2
.\.venv\Scripts\python.exe -X utf8 tools/validate_engineering_refactor.py --phone C:/Users/32673/DevEcoStudioProjects/AllDayRecording --output outputs/engineering-followup-final-r4
.\.venv\Scripts\python.exe -X utf8 -m allday_asr.cli engineering-recovery --help
```

正式测试使用全新临时目录，完整实际命令/cwd/退出码在最终 results.json。恢复子命令仅展示调用形式，`--state-dir <指定状态目录>`，retry 还需 `--session-id`，coalesce 还需两个明确 request ID 和 `--actor`；daily 两项需 `--first-date` / `--last-date`，repair 还需 `--actor`。本轮 CLI help 退出 0，未对生产目录运行修复命令。

| 轮次 | 实际结果 | 证据（outputs/ 下） |
|---|---|---|
| FUP-01 关联修正后 | 54 passed，退出 0 | engineering-followup-f01-r2.log / xml |
| 同目标 CAS / 独立修改 | 2 passed，退出 0 | engineering-followup-f01-extra.log |
| 未知格式/会话不可用最终补充 | 20 passed / 44 deselected，退出 0 | engineering-followup-snapshot-format.log / xml |
| FUP-02 关联 | 18 passed / 45 deselected，退出 0 | engineering-followup-f02-r2.log / xml |
| FUP-03 关联 | 53 passed，退出 0 | engineering-followup-f03.log / xml |
| 跨能力关联 | 190 passed / 1 failed；唯一为新增同目标 fixture 参数错误，随后修正单项通过 | engineering-followup-associated.log / xml |
| 完整 r1 | 935 passed / 1 failed / 1 skipped；其余五项退出 0；唯一失败为盲测报告原子 replace 的 Windows WinError 5 | engineering-followup-final-r1/results.json、逐项 stdout/stderr / pytest.xml |
| 盲测报告文件锁回归 | 23 passed，退出 0；临时 Win5/32 重试和永久失败保留旧报告 | engineering-followup-blind.log / xml |
| 完整 r2 | 939 passed / 1 skipped，166.46 秒；六项退出 0（尚未纳入最后新增的 8 个快照边界案例） | engineering-followup-final-r2/results.json、逐项 stdout/stderr / pytest.xml |
| 完整 r3 | 946 passed / 1 failed / 1 skipped，162.95 秒；其余五项退出 0；唯一失败为设备凭据文件原子 replace 的 WinError 5 | engineering-followup-final-r3/results.json、逐项 stdout/stderr / pytest.xml |
| 设备凭据文件锁/认证回归 | 8 passed，退出 0；临时 Win5/32 重试、持续故障不覆盖注册表及原签名/重放拒绝测试 | engineering-followup-device-auth.log / xml |
| 最终完整 r4 | 950 passed / 1 skipped，163.59 秒；六项退出 0 | engineering-followup-final-r4/results.json、逐项 stdout/stderr / pytest.xml、frontend-dist/ |
| 实际 Phone 消费 | 退出 0，HOST_NOT_DEVICE | engineering-followup-phone-consumer-r2/ |
| Phone debug 构建 | BUILD SUCCESSFUL，15.062 秒，退出 0 | engineering-followup-phone-build.log |
| Phone/entry Hypium 主机测试 | Phone 102/102，entry 68/68，合计 170/170，32.473 秒，退出 0 | engineering-followup-phone-host-r2.log |

Phone 构建/测试在 Phone checkout，使用已安装 DevEco Studio Node/Hvigor：

```powershell
$env:DEVECO_SDK_HOME = 'C:/Program Files/Huawei/DevEco Studio/sdk'
$env:JAVA_HOME = 'C:/Program Files/Huawei/DevEco Studio/jbr'
$env:JAVA_OPTS = '-Djdk.util.zip.disableZip64ExtraFieldValidation=true'
& 'C:/Program Files/Huawei/DevEco Studio/tools/node/node.exe' 'C:/Program Files/Huawei/DevEco Studio/tools/hvigor/bin/hvigorw.js' --mode project -p product=phone -p buildMode=debug assembleApp --no-daemon
$env:HOST_TEST_TIMEOUT_MS = '120000'
& 'C:/Program Files/Huawei/DevEco Studio/tools/node/node.exe' tools/quality/run-host-tests.mjs
```

第一次 Phone 主机启动因 SDK `.test/phone` 编译输出与 `.test/default` 启动目录映射超时；日志 engineering-followup-phone-host.log 保留。核对 modules.abc 比修改源码新后，将刚编译的 product 输出复制至 SDK launcher 目录（与既有 gate 的处理一致），再完整运行该 gate；fresh-report 校验通过。仅增加主机启动等待时间，没有调整业务模型预算、删断言或使用旧报告。

FUP-01 开发关联失败暴露 reserved resolved_inputs 被恢复代码复制、operation_history 字段命名错误，修正后通过；所有开发失败日志保留。完整 r1 的文件锁故障采用已有两秒有界 replace_atomic（仅 Windows 5/32）修复报告 JSON/Markdown 发布，持续错误仍显式抛出；新增故障测试证明旧报告未丢失，没有改盲测真值/隔离逻辑。完整 r3 的设备凭据文件同类 Windows 故障也复用该 helper，新增真实注册和永久故障测试；认证算法、签名、防重放规则未修改。

最终 source-manifest.json 包含 **93 个**当前新增/修改的源代码和测试文件（90 ASR + 3 Phone，包含首轮），测试后逐项 SHA-256 再核对一致。delivery-manifest.json 追加最终文档与本轮核心证据校验摘要。首轮 engineering-final-r4 和第二轮 r2/r3 的旧 manifest 保留为各轮历史快照，不冒充最终全部文件。完成能力验收不等于生产历史数据已修复。

## Daily 状态转移与预算

| 当前状态 | 触发/下一状态 | attempt | 自动重试与 owner 约束 |
|---|---|---|---|
| 不存在 | NO_SOURCE 不建 job；CURRENT_COMPLETE/EMPTY→SUCCEEDED；UPSTREAM_INCOMPLETE→WAITING_UPSTREAM；CURRENT_DAY→DIRTY_SOURCE_CHANGED；需生成→PENDING | 0 | reason_code/classification 决定，与 reason 文案无关 |
| WAITING_UPSTREAM / DIRTY_SOURCE_CHANGED | 上游完整/跨日且相同 fingerprint→PENDING | 不回退 | 未耗尽且 automatic_retry_blocked=0 |
| PENDING / FAILED_RETRYABLE | 到 available_at，来源 identity 匹配→RUNNING | +1 | DB 事务 claim，生成新 lease_owner；attempts<attempt_limit |
| RUNNING | heartbeat 留在 RUNNING | 不变 | 同 job、RUNNING、同 owner，否则拒绝 |
| RUNNING | 完成→SUCCEEDED；可重试失败→FAILED_RETRYABLE；耗尽/terminal/显式 backfill 失败→FAILED_TERMINAL | 不变 | finish 必须同 owner；失败不覆盖已发布业务内容，retryable 延后 5 分钟 |
| RUNNING | 获取独占 OS owner 后 recover→FAILED_RETRYABLE 或 FAILED_TERMINAL | 不回退 | 旧执行已不持有锁；原 owner 不能 finish 新领取的 job |
| PENDING / FAILED_RETRYABLE | 明确 provider outage hold→WAITING_PROVIDER | 不变 | 禁止自动尝试，不标成已调用失败 |
| FAILED_TERMINAL / FAILED_RETRYABLE / WAITING_PROVIDER | 用户显式 Daily retry→PENDING | 不清零；旧总数+2 为下一上限 | 沿用原 Daily 人工预算语义；普通 observe/restart 不扩次数。底层 terminal receipt 不被删除或重置 |
| 任意旧 identity | source fingerprint/generation_version 改变→新 job identity | 新执行从 0 | 旧 job/receipt 保留；新历史远程任务仍需要 backfill 授权 |

## 迁移、旧格式和回滚

追加私有 migration **v030**，未改 v001–v029：增加采样 queued_at、将旧等待年龄起点设为升级时刻、替换现有 coalescing 触发器以同时写年龄、补人工人物引用的 dirty 日期。旧回填期间表尚无 queued_at 时保留旧 enqueue SQL。库内 checksum 仍严格；失败的实际 v030 SQL 会回滚字段/触发器/version。Phone wire DTO/schema/release lock 字节保持不变。

generation scope 的模型 input_snapshot version=1、产品规则 version=2/product-reminder：新模型记录保存实际输入；人工命令保存读取的 expected revision；旧 pending 缺可信快照拒绝接受并提示重新生成。已接受事实不通过升级自动重写。v2 execution 使用独立文件 identity；旧 runner receipt 的解释和预算仍保留。

workflow sidecar schema 3 独立于主库，以每来源标记和事务导入原 JSON；确定的 legacy request ID 防止重复导入，同步保留文件诊断。迁移后 sidecar 为权威，旧 JSON 不代表最新状态。**部署应同时停止并升级 receiver/desktop，再启动新代码。不能用直接 checkout 旧程序回滚 schema 30、sidecar schema 3 或新 workflow 请求**；应恢复升级前一致性备份，或另行实施可审阅的导出/降级。无生产数据库参与迁移试验，也未自动修复历史事实。

## 首轮命令与证据（历史记录）

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/reproduce_engineering_baseline.py --output outputs/engineering-baseline-final
.\.venv\Scripts\python.exe -X utf8 tools/validate_engineering_refactor.py --phone 'C:\Users\32673\DevEcoStudioProjects\AllDayRecording' --output outputs/engineering-final-r4
```

统一脚本内部运行 pytest -q、Ruff src/tests/tools、sync_v3_contracts --check、npm test、vue-tsc -p tsconfig.v3.json --noEmit、Vite v3 build。每轮新目录，JSON 中保存实际绝对命令与 cwd。当前新固定 fixture 的 Daily input hash 为 `1f85d17ebd3813d95f26cc4822539693a241d43b1b75451989eee4b821e90dea`，独立旧基线产生同值。

| 轮次 | 命令退出码与结果 | 日志 |
|---|---|---|
| 旧基线最终复现 | pytest 1（预期）：8 failed，0 error；复现脚本 0，old_defects_reproduced=true。含人物/任务的 7/90/365 天同步 fixture | outputs/engineering-baseline-final/results.json、pytest.stdout.log、pytest.stderr.log、pytest.xml |
| 完整 r1 | pytest 1：885 passed / 4 failed / 1 skipped；其余五项全部 0 | outputs/engineering-final-r1/ 下逐项日志及 results.json |
| 完整 r2 | pytest 1：891 passed / 2 failed / 1 skipped（早期回填缺 queued_at）；其余五项全部 0 | outputs/engineering-final-r2/ 下逐项日志及 results.json |
| 完整 r3 | pytest 1：894 passed / 1 failed / 1 skipped；其余五项全部 0。唯一失败为新增删除日期测试调用 list_invalidations 缺少 limit 参数，修正后单项通过 | outputs/engineering-final-r3/ 下逐项日志及 results.json |
| 完整 r4 | pytest 0：895 passed / 1 skipped，280.44 秒；Ruff、contracts、frontend-tests、frontend-types、frontend-build 全部 0；统一脚本 0 | outputs/engineering-final-r4/ 下逐项 stdout/stderr、results.json、pytest.xml、frontend-dist/ |

首轮最终代码及测试的 78 个新增/修改文件和 SHA-256 在 `outputs/engineering-final-r4/source-manifest.json`；当时全部在测试结束后再次核对一致。该轮 `delivery-manifest.json` 另包含当时三份更新的工程文档及校验结果，不代表本轮补修后的文件。输出目录是本地验收证据，不是运行时输入；正式测试无需历史 outputs 即可构造数据。

r1 的 Windows 临时文件 replace WinError 5 在隔离重跑通过，新增只针对 WinError 5/32 的两秒有界原子替换重试；持续写故障仍抛出。其他 r1 失败分别是 provider 校验分类、年龄触发器额外一次队列写及取消异常的跨层 import，均修正并保留原断言。r2 发现旧迁移在 v030 前调用仓储，新增兼容分支，旧迁移/成本关联 **11 passed**。修复关联套件 **86 passed**；Daily owner/端口/范围套件 **36 passed**；自有 native tree、staged artifact、关闭后恢复套件 **9 passed**；fake-clock 长采样 **1 passed**；执行 ID 的 receipt、缓存、generation scope/provenance 关联套件 **49 passed**。较早 tmp_path 沙箱 WinError 5 使用获准的本地测试权限运行，未关闭相关测试。其他临时开发失败是 fixture 初始化/状态/字段及 Windows 子进程清理顺序问题，均修正；正式全量失败日志始终保留。

### 行为测试入口

- `tests/test_engineering_refactor.py`：R1 两个发布窗口、来源/上下文失效、稳定 event 再生成、R2 身份/冻结时间/预算/执行记录、R4 崩溃恢复/并发/旧状态、诊断、迁移回滚、Daily 查询 hash 和真实 7/90/365 天 sync、采样 fencing、native 六阶段挂起/关闭及产物恢复。
- `tests/test_engineering_sample_lifecycle.py`：fake clock 的 200 秒慢 embedding、双 worker 不重复领取、close 停止领取与续租。
- `tests/test_daily_event_layer.py`、`tests/test_daily_automation.py`：删除日期保留已发布内容并失效、独占 owner、处理预算、模型关闭、dirty ACK 与队列转移；关联原有 Daily 测试继续运行。
- `tests/test_v3_architecture.py`：真实 AST import/SQL 依赖检查；原人物 policy CAS、purity、设备审核、确认任务测试保留。
- `all_day_recording_front/tests/engineering-behavior.test.mjs`：实际 Vue ReviewsView 挂载、候选局部失败/重试/操作保护、共享请求取消、TTL/容量淘汰、编辑后查询失效；原交互竞态测试继续运行。

## NOT_RUN 与明确边界

| 项目 | 状态和原因 |
|---|---|
| 真实外部模型可达性、质量、耗时 | NOT_RUN：本次使用 fake client，不发送真实录音、不开启远程调用 |
| 实机 GPU 长录音、显存回收、音频设备 | NOT_RUN：自有 CPU 子进程证明 deadline/进程树/owner 协议，不证明 GPU 质量或回收效果 |
| 原生 embedding provider 的设备级中断 | NOT_RUN：采样续租有限，close 两秒后暴露 shutdown_incomplete，旧线程不得续租/发布；不承诺 Python 线程能停止不合作的 GPU provider |
| Phone/Watch 后台、重连、Calendar、设备系统生命周期 | NOT_RUN：完成双仓库契约核验、修改后 Phone debug 构建、170 项主机测试和实际 SQLite 消费测试；后台/设备重连/Calendar/系统生命周期实机未运行 |
| 真实用户 365 天负载、多日 soak | NOT_RUN：用 7/90/365 天合成数据、SQL 次数/范围、fake clock、缓存容量验证趋势；没有读取生产录音或进行多日运行 |
| 历史错误识别/生产修复/历史远程 replay | NOT_RUN：已提供并在合成库验证候选恢复、重复请求报告/审计合并、最多 14 天的摘要补发；未对生产执行任何上述操作，升级不会自动覆盖已接受事实或扩大历史生成 |

原 `test_private_historical_event_regression` 的 private local integration opt-in skip 继续保留；它不能用于宣称全部外部集成已通过。文档与产品保持重算账本的人工处理语义，不承诺尚未实现的通用后台自动修复。
