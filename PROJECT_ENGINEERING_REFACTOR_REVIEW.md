# 工程重构独立复核记录

日期：2026-10-07。复核对象：以 `87532be` 为基线的当前未提交工作区改动。

首次复核结论（历史）：整改方向正确，关键风险已有实质改善，现有完整验收矩阵独立复跑通过。但本次补充场景复现了三个行为缺口，暂不建议将 ENG-01、ENG-03、ENG-05 视为完全验收通过。先补上以下回归测试并修复，再更新完成状态。

首次独立复核阶段没有修改业务代码。复现使用独立合成数据库，没有调用真实远程模型、GPU 模型或生产数据；没有验证 Phone/Watch 实机。

后续修改依据已补入 [修改指导第 10 节](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_GUIDE.md#10-独立复核后的补修指导第二轮)，对应 FUP-01/02/03。本文保留首次发现时的复现事实与第二轮关闭记录。后续复核发现模型批次编辑 P2 遗漏，曾重新打开 FUP-01 / ENG-01；现已按第 11 节完成 FUP-01B 并恢复 DONE，实际关闭证据见本文末尾及进度记录。发现时事实与旧验收历史保留。

## 1. P1：确认一条提醒导致同录音的另一条独立提醒永久冲突

关联任务：ENG-01。

定位：`src/allday_asr/v3/application/reminder_extraction.py:191`；`src/allday_asr/v3/application/generation_inputs.py:34`。

`extract_product()` 为每个独立规则候选保存整份 reminder 输入快照，其中包含会话的全部 active reminders。确认第一条候选后，新日程改变了这个集合；第二条候选虽然原始证据完全没有改变，仍会在 `accept_proposal()` 的快照校验中失败。`confirm()` 又把它落成终态 conflict。由于 product_source_key 幂等回放保留这个决定，重新提取也不能恢复第二条。

实际复现：

1. 同一录音包含两条 self utterance：“我明天下午三点给老师发材料”和“我明天下午四点给同学发照片”。
2. 调用真实 `extract_product()`，产生两条 pending_confirmation 候选。
3. 按顺序调用真实 `reminders.confirm()`，不修改任何来源。
4. 第一条为 confirmed，第二条为 conflict；最终只有一个 schedule。
5. 再次 `extract_product()`，仍返回 confirmed、conflict。

证据：`outputs/engineering-review-reminder-siblings/repro-result.txt`。

修复指导：规则提取的快照只覆盖实际使用的原始证据、身份、录制时间与时区，不应加入其未使用的全部活动提醒。模型候选如果确实读取了活动提醒上下文，则需要区分外部上下文变化与同批已接受输出，保持来源校验，不能简单关闭快照校验。同时明确历史误判 conflict 候选的恢复办法，避免继续被幂等记录锁死；不要自动重放用户明确忽略或完成的决定。

验收要求：

- 同录音两条独立产品候选都能顺序确认，产生两个日程。
- 确认后重试、重启、重新提取均不重复建任务。
- 真实来源修订、身份变化仍会阻止旧候选发布。
- 补查一次模型生成多个候选的逐个确认场景。

## 2. P1：旧 JSON 会话升级后，一次人工重试产生两条 durable request

关联任务：ENG-03。

定位：`src/allday_asr/v3/adapters/transfer/automation_state.py:52`。

`initialize()` 每次扫描保留的旧 JSON，并根据 SQLite 中的当前状态决定是否创建 legacy request。迁移后的新请求会将当前状态设为 retry_requested；随后 `consume_retry_requests()` 再调用 initialize，就把这个新状态误当作旧请求导入，额外生成一个 legacy ID。UUID 请求和 legacy 请求的主键不同，INSERT OR IGNORE 无法去重。

实际复现：

1. 写入旧格式 needs_attention 会话 JSON，没有旧 retry request 文件。
2. initialize 迁移，然后只调用一次 request_retry。
3. consume_retry_requests 返回 **2** 条 pending 请求：一个 UUID、一个 legacy ID，session 和时间相同。

后果：同会话 active 检查只能延后第二条请求，不能确认它已被消费；第一次执行结束后，余下请求仍可再次排队，额外消耗一次人工重试并重复执行流水线。

证据：`outputs/engineering-review-legacy-retry/repro-result.txt`。

修复指导：将旧状态和旧请求的导入限定为明确的一次迁移，持久记录迁移完成或原始 legacy 请求身份。不要使用迁移后的实时 SQLite 状态合成 legacy 请求。保证迁移标记、状态导入、请求导入在同一事务提交。

验收要求：

- 已迁移且保留 JSON 的会话，一次新人工重试始终只有一个请求。
- 重复 initialize/statuses/consume、进程重启、并发请求均不增加请求数。
- 旧 retry_requested 有/无 request 文件仍能恢复一次。
- 第一轮执行完成后再次 poll 不出现第二次排队；下一次合法人工请求仍可正常创建。

## 3. P1：Daily 失效只写数据库，没有同步给手机

关联任务：ENG-05。

定位：`src/allday_asr/v3/application/daily_events.py:50`。

新增 `mark_daily_stale()` 只插入 invalidation_events，没有同时追加 daily_summary 的同步变化。`MobileSyncService.synchronize()` 从 change_events 的持久 payload 返回数据，不会根据 invalidation_events 重新计算摘要状态。因此桌面读取已经 stale，增量同步和新设备 bootstrap 仍可能收到旧的 active 摘要。

实际复现沿用新增删除日期测试的真实 service/coordinator 路径：先发布 2026-09-01 摘要，再将其来源 utterance 设为 stale 并 tombstone 会话；模型关闭，调用 coordinator.catch_up。

观测结果：

```text
db_derivation_status = stale
latest_sync_derivation_status = active
new_sync_event_count = 0
```

对于不会自动远程重算的历史日期，这种不一致可能一直保留。现有删除日期测试只断言数据库有失效记录，没有覆盖同步结果。

证据：`outputs/engineering-review-daily-sync/repro-result.txt`。

修复指导：在同一事务中持久化失效状态与相应同步投影，沿用既有 DTO，并验证客户端如何处理同内容 revision 的状态变化。保持原摘要内容和历史，重复维护不得无限追加等价变更。不能依赖未来重新生成摘要来补发失效。

验收要求：已有 cursor 的增量同步和新设备 bootstrap 均能看到 stale；重复 catch_up 幂等；内容没有被清空；确认任务不受牵连。补充真实删除入口的端到端测试。

## 已通过的独立验证

执行命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 tools/validate_engineering_refactor.py --phone 'C:\Users\32673\DevEcoStudioProjects\AllDayRecording' --output outputs/engineering-independent-review
```

| 项目 | 结果 |
|---|---|
| pytest | 895 passed、1 skipped，158.72 秒 |
| Ruff | 退出码 0 |
| Phone 契约一致性检查 | 退出码 0 |
| 前端测试 | 退出码 0 |
| Vue/TypeScript 类型检查 | 退出码 0 |
| Vite 构建 | 退出码 0 |

完整日志、JUnit 和命令记录：`outputs/engineering-independent-review/`。三个补充复现未加入正式测试套件，因此完整矩阵通过不覆盖上述缺口。

本轮值得保留的成果包括：原始模型输入快照与事务内复核、按输入隔离 execution 预算、SQLite retry outbox、Daily 后台有界维护、native 自有进程超时控制，以及前端局部失败提示。后续优先修复以上三个具体场景，补齐对应回归测试，再复跑相关套件与统一矩阵；无需因此推倒整轮重构。


## 第二轮关闭记录（2026-10-07，历史）

FUP-01/02/03 已实现并完成正式行为、兼容及恢复验证。完整最终矩阵六项退出 0，后端 **950 passed / 1 skipped，163.59 秒**；Phone debug 构建、170 项主机测试和实际 SQLite 消费验证通过。详情及案例映射见 [第二轮补修记录](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_PROGRESS.md#独立复核与第二轮补修2026-10-07历史验收)。原复核日志、历史 manifest 和发现时结论保留；生产历史修复与实机验证未执行。


## FUP-01B 模型同批候选修改遗漏（P2，发现时记录）

日期：2026-10-07。发现时关联任务 ENG-01 / FUP-01 改为 REOPENED，FUP-01B 为 TODO；后续完成记录见下节。其余第二轮完成记录保留。

定位：[reminder_commands.py:189](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/reminder_commands.py:189)。模型生成两个独立 CREATE_TASK 候选 A/B；确认 A 后直接确认 B，结果为两个 confirmed、两个日程；相同前置条件下修改 B 的标题并确认，抛出 GenerationSourceConflict，最终只有 A 的日程。来源和外部上下文未改变。

原因：直接确认使用原 generation_id 验证同批输出，modify 的前置校验没有携带该身份。替代 generation 提交和接受还涉及新的身份，因此仅在前置校验补一个参数不足以贯通来源证明。应从可信原候选/批次关系贯通三个环节，保持真实来源冲突和 revision CAS，禁止把原快照替换为当前来源。

实际复现：[result.json](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/outputs/engineering-followup-review-model-edit/result.json)。正式修复步骤、B01～B08 测试和关闭标准见[修改指导第 11 节](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_GUIDE.md#11-fup-01b模型同批候选的修改链路收尾当前执行入口)。发现时该案例仅为独立复现；现已加入正式 B01 测试，完成修复前失败、修复后通过验证。

收尾前独立验证：完整矩阵六项通过，950 passed / 1 skipped、169.29 秒，日志 `outputs/engineering-followup-independent-review/`；Phone SQLite 消费再次通过，日志 `outputs/engineering-followup-phone-independent-review/`。93 个源码/测试摘要与最终 source manifest 一致。Phone 构建和 170 项主机测试仅核对既有日志，未重跑；真实模型、GPU、实机和生产历史修复仍未执行。

判断：原三个 P1 的针对性修复和证据可信，本次需要收尾的是模型批次编辑入口。可以继续实机验证准备，无需扩大架构重构范围。该独立发现阶段仅编辑文档；后续代码修复与新验收结果另列，历史日志未覆盖。

## FUP-01B 实施关闭记录（2026-10-07）

**FUP-01B 已关闭，FUP-01 / ENG-01 恢复 DONE。** B01 在未修复代码上按预期 GenerationSourceConflict 失败，之后三环节可信来源关联修复通过；B01～B08 已覆盖实际提交/接受、事务间变更、原 modified 写入后故障回滚、Barrier 并发、持久重启、旧关联与桌面/Phone 等价结果。额外通用 proposal accept 半发布问题也先复现，再阻止绕过 reminder review 回调。严格同批证明、真实来源冲突、目标 revision CAS 与不可变历史保护保留。

最终完整矩阵 `outputs/engineering-fup01b-final-r2/results.json` 六项退出码均为 0：后端 **1010 passed / 1 skipped，184.50 秒**，Ruff、Phone 契约、前端测试、类型检查及构建通过。关联套件 202 项、最终提醒/TLS 138 项通过。首次完整复跑遇到 TLS PEM 的 Windows 临时锁；复用既有两秒有界 replace 重试后，瞬时与持续失败测试及原 CA 稳定测试通过。所有失败日志与原 manifest 保留，未以重跑覆盖失败记录。

本轮 12 个源码/测试变更，最终 source manifest 99 项验收后 SHA-256 再核对一致；3 项 Phone 文件哈希与此前基线一致，本轮未改 Phone 仓库。未重跑 Phone 构建、170 项主机测试或独立 Phone SQLite 消费脚本；真实模型、GPU、OS Calendar/Phone/Watch 实机和生产历史修复未执行。详细函数映射、命令、失败过程及证据见 [FUP-01B 关闭记录](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_PROGRESS.md#fup-01b-关闭记录2026-10-07)。
