# 工程质量整改与模块重构指导

版本：1.5 · 更新日期：2026-10-07 · 基于审计代码基线：`87532be`

依据：[PROJECT_ENGINEERING_AUDIT.md](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_AUDIT.md)。审计解释问题和证据，本文规定后续如何修改、如何控制范围、如何验收。若源码已演进，先核对实际调用链；发现问题已修复时，用行为测试证明后关闭对应任务，不重复改造。

**总体路线：Level 2 模块级重构，REFACTOR WHILE BUILDING。先修一致性与恢复缺陷，再整理结构。**

当前状态：第 11 节 FUP-01B 已完成，FUP-01 / ENG-01 恢复 DONE，其余任务保持 DONE。最终后端 **1010 passed / 1 skipped，184.50 秒**；Ruff、Phone 契约、前端测试、类型检查及构建均退出 0，证据为 `outputs/engineering-fup01b-final-r2/`。关联套件 202 项、最终提醒/TLS 定向检查 138 项通过。Phone 仓库源码未改，本轮未重跑 Phone 构建、170 项主机测试或独立 SQLite 消费脚本；历史通过记录保留。生产历史处理、真实模型、GPU 和实机验证仍为 NOT_RUN。

问题证据见 [PROJECT_ENGINEERING_REFACTOR_REVIEW.md](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_REVIEW.md)；执行记录、历史验收和 NOT_RUN 边界见 [PROJECT_ENGINEERING_REFACTOR_PROGRESS.md](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/PROJECT_ENGINEERING_REFACTOR_PROGRESS.md)。真实模型、GPU 长录音及 Phone/Watch 实机未运行。

## 1. 范围、原则与完成口径

### 1.1 修改范围

主要修改当前 ASR 仓库的后端、桌面前端及其测试。Phone/Watch 仓库仅在确实涉及 wire contract 或消费行为时协调修改，不把本次工作扩大成两端重写。

下文的源文件清单是定位入口，不是要求机械修改每个文件。新增文件名、类名可按实际实现选择；必须实现所列行为约束。

### 1.2 必须保持的业务不变量

1. 原始录音、历史转录、人工标注、声纹真值与盲测隔离语义保持可追溯。
2. 已确认提醒/任务仍由用户意图决定。来源变化可以标记待复核，不能自动取消、改期、删除 Calendar 项或悄悄覆盖任务。
3. 模型输出必须绑定其实际读取的证据版本；不能通过重新读取当前 revision 来给旧输出“更新来源”。
4. 文件采用不可变内容寻址；耗时 copy/hash/fsync 与推理不占用 SQLite writer transaction。事务内完成版本校验和业务发布。
5. 重试可以重复执行准备工作，但不能重复发布同一操作、回退版本、重置预算或丢失已经持久接受的请求。
6. 历史批量回放与普通增量维护严格区分。普通启动、同步、重试、升级不能暗中触发全历史远程模型生成。
7. 既有迁移不可原地修改；需要 schema 变化时追加迁移，并保留旧数据/receipt 的可解释性。

### 1.3 本轮不做的重写

不替换 SQLite/UOW，不引入微服务，不为统一形式更换 Web 框架或全量仓储，不重写稳定 ASR/声纹算法，不删除仍承载来源身份的 legacy_ref，不为缩短文件行数继续机械拆 Mixin。

测试、指标和目录整洁度用于证明行为改善，不作为替代行为正确性的目标。不得通过放宽断言、删除回归测试、扩大 ignore 或移除超时/预算来“修复”。

### 1.4 任务何时算完成

一个任务只有同时满足以下条件才能从 TODO/IN_PROGRESS 变为 DONE：

- 对应旧问题有正式行为测试；重要缺陷的测试已确认能在旧行为上失败。
- 所有调用入口都使用新规则，旧旁路已移除或明确兼容封装。
- 新旧数据、重启、失败与重试行为有确定定义，必要迁移经过临时库验证。
- 相关检查通过，状态记录包含真实命令、结果及未覆盖边界。
- 文档同步到最终实现，没有留下“计划完成”冒充“实际完成”。

REOPENED 表示已有实现被新证据发现仍有缺陷；保留原验收历史，新增失败案例，修复并重新验收后才恢复 DONE。BLOCKED 只表示存在明确外部依赖，需记录原因；条件性任务不做时记 DEFERRED 并说明触发条件，不能算作已修复。

## 2. 任务清单与执行顺序

| ID | 对应审计 | 优先级 | 任务 | 所属阶段 | 当前状态 |
|---|---|---|---|---|---|
| ENG-01 | R1 | P0 | 固化生成输入快照，修复提交/接受竞态 | Phase 1 | DONE（FUP-01B 已关闭） |
| ENG-02 | R2 | P1 | 修复 generation/batch/job/retry 身份和预算作用域 | Phase 1 | DONE |
| ENG-03 | R4 | P1 | 修复手动重试 claim/ack 与崩溃恢复 | Phase 1 | DONE |
| ENG-04 | R7 | P1 | 补齐关联错误、错误分类与 worker 健康状态 | Phase 1 起贯穿执行 | DONE |
| ENG-05 | R3 | P1 | 统一 Daily 有界维护入口，移除同步全历史刷新 | Phase 2 | DONE |
| ENG-06 | R3、P2 | P1/P2 | 批量读取 Daily 输入，显式化队列状态规则 | Phase 2 | DONE |
| ENG-07 | R5 | P1 | 原生推理期限、取消、关闭与失效 owner 隔离 | Phase 3 | DONE |
| ENG-08 | 并发风险 | P2 | annotation sample lease 与长期运行验证 | Phase 3 | DONE |
| ENG-09 | R6、跨层问题 | P1/P2 | 收口人物/审核/证据转换和 repository 边界 | Phase 4 | DONE |
| ENG-10 | 前端 P2 | P2 | 修复局部失败表达与缓存生命周期 | Phase 4 | DONE |
| ENG-11 | recompute P2 | P2 | 明确重算账本的产品语义与恢复入口 | Phase 4 前完成设计判断 | DONE |
| ENG-12 | 测试与文档 | P1 | 建立可重复验收入口与最终证据记录 | 各阶段及收尾 | DONE |

**当前执行顺序**：第 11 节已完成 B01 失败复现 → 三环节与旁路修复 → B01～B08 回归 → 关联检查及完整验收。第二轮 FUP-01 → FUP-02 → FUP-03 和首轮 ENG 实施顺序保留为历史；不重复完成项。

ENG-01 未关闭前，不扩大自动接受模型事实的使用范围。其他独立功能可继续，不要求全项目停工。

## 3. Phase 1：一致性、执行身份与恢复

### ENG-01 — 模型输入快照与发布前置条件

**入口文件**：

- [event_extraction.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/event_extraction.py)
- [knowledge_generation.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/knowledge_generation.py)
- [knowledge_queries.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/knowledge_queries.py)
- [knowledge_resolution.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/knowledge_resolution.py)
- [knowledge.py：领域模型](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/domain/knowledge.py)
- [knowledge_repository_events.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/knowledge_repository_events.py)

**目标行为**：模型读取 revision 1，期间原文变为 revision 2，则 revision 1 的结果不得作为 revision 2 的有效事实写入。该约束在“生成后提交”和“proposal 等待审核后接受”两个窗口均成立。

**实施步骤**：

1. 在读取模型输入的同一个只读快照中，捕获模型真正使用的来源 ID、revision、有效状态，以及影响输出的身份/任务上下文版本或指纹。不要只记录模型最后引用的几个 evidence ID；上下文变化也可能改变结论。
2. 为输入快照定义明确类型和版本。优先复用已有 generation scope 存储，只有需要独立查询/约束时才新增列或表。revision 真值来自服务端读取，不信任模型回包自行声明。
3. 模型调用在事务外执行。`submit_generation` 的写事务内比较快照与当前来源；不匹配时给出明确 source conflict，禁止自动接受。
4. 将原始输入快照持久保存到 generation，proposal 接受时再次读取并核对。比较必须与 event/memory/dependency 的写入处于同一 UOW，避免检查与提交间再次竞态。
5. evidence link 和 derivation dependency 使用已经验证的原始快照版本。禁止在 `_accept_event` 中用新 revision 替代旧版本后继续提交。
6. 核对 reminder、memory、daily 和人工提交路径，明确各自的来源契约。Daily 已有 hash CAS，应保留；不要为了统一接口撤掉其保护。人工命令应使用命令读到的 expected revision，不伪装成模型快照。
7. 采用显式冲突结果，保持已发布内容。需要更新 proposal 状态时，确保该状态不会因抛异常导致事务回滚后消失；也可以先采用稳定的拒绝接受结果，不强制新增状态。

**旧数据兼容**：缺少可信“实际生成输入快照”的旧 pending proposal 不得自动回填当前 revision 后接受。优先进入需重新生成/重新核对的路径，并提供可理解原因。历史 generation 的 `resolved_inputs` 可能是在提交时才捕获，不能一概当作实际推理版本证明。已经接受的用户事实保持原状；历史错误识别/修复另立可审阅方案，不混在启动迁移中。

**验收案例**：

| 编号 | 场景 | 必须断言 |
|---|---|---|
| 01-A | 模型执行中修订 utterance | 旧结果被拒绝或明确冲突；没有 active 错误事件/依赖 |
| 01-B | proposal 提交后、接受前修订 | 人工与自动接受入口都不能附上新 revision 偷渡 |
| 01-C | 原始来源被 supersede、撤销或变为不可用 | 不能接受为当前有效证据 |
| 01-D | 来源及上下文均未变化 | 正常提交/接受，依赖版本与输入版本一致 |
| 01-E | 仅变更未参与模型输入的无关对象 | 不误报来源冲突 |
| 01-F | 重复提交/接受、异常回滚 | 不生成重复 operation，不留下半套依赖/同步投影 |
| 01-G | 旧 pending proposal 无可信快照 | 有明确兼容结果，不自动补“当前 revision” |
| 01-H | 已确认任务的来源变化 | 任务意图/Calendar 语义保留，来源待复核仍可识别 |

优先扩展现有 V32 knowledge/semantic event 和提醒测试。使用 barrier/回调实现确定性交错，不靠 sleep 碰竞态。

### ENG-02 — 执行身份和预算生命周期

**入口文件**：[model_execution_runner.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/codex/model_execution_runner.py)、[event_generator.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/codex/event_generator.py)、[reminder_generator.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/codex/reminder_generator.py)、[insight_generator.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/codex/insight_generator.py)、[automation_retry.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/transfer/automation_retry.py)。

**先明确四种身份**：

| 身份 | 含义 | 稳定性要求 |
|---|---|---|
| 业务实体 | session/date/person | 长期存在，不能直接充当有期限的 batch |
| generation/execution | 一次针对明确输入的业务执行 | 在首次模型调用前确定并持久化；恢复不变 |
| batch/job | 此次执行的预算边界与具体模型请求 | 类别、输入快照、producer/协议等纳入作用域 |
| attempt | 同一 job 的一次尝试 | 只递增；重试不新建 generation 来绕预算 |

**实施要求**：

1. 预算以一次业务执行为边界，不以 session 全生命周期为边界。event/reminder/insight 必须避免无意共享同名 batch；若需要共享总预算，定义显式父执行预算。
2. 新来源 revision 或显式重新生成可开启新的 execution；同一 execution 的网络重试、自动重试、进程重启继续使用旧 ID/预算。
3. 明确区分“重试当前执行”与“重新生成”。默认 retry 不重置终止预算。若增加重新生成入口，持久记录新执行及其来源，不能在内部异常重试中隐式开启。
4. 输入指纹必须规范化，避免 `now_utc`、随机 ID、序列化顺序变化让等价重试逃出同一预算。确实影响业务语义的当前时间应作为执行快照固定，而不是每次 retry 取新时间。
5. 检查 runner 的 job/cache key 与新 execution 身份的关系。若相同 prompt 已耗尽旧 job，新的显式 execution 不能被意外绑定到旧终止状态；同一次 retry 又不能绕开旧 job。明确缓存命中是否共享以及 provenance/预算记账方式。
6. 保留超时、最大次数、字节/token/call 预算、独占子进程、receipt、取消与 fencing。不得通过改成无限预算消除报错。
7. runner 终止原因传播到调用方；automatic workflow 不应把所有 terminal failure 一律继续自动重试。

**旧 receipt 兼容**：旧 batch 不删除、不篡改预算。定义版本化 identity；新输入按新规则进入新执行，旧执行恢复仍解释其旧状态。相同输入明确重新生成的行为要有记录和测试。

**验收**：同 session 不同 revision 间隔超过 30 分钟可执行；同 job 反复 timeout 仍耗尽；重启预算不回满；同输入时间戳抖动不绕预算；不同 task 不意外互相阻断；明确重新生成可在旧终止执行之后开始；并发相同 execution 不重复启动远程；缓存返回仍能追溯真实输入/结果。

### ENG-03 — 手动重试持久领取与确认

**入口文件**：[automation_state.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/transfer/automation_state.py)、[automation_retry.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/transfer/automation_retry.py)、[automation.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/transfer/automation.py)。

**目标协议**：接受请求并持久化 → 可恢复地领取 → 持久记录已调度/关联执行 → 确认完成领取。任何崩溃点允许重放，但不能丢失用户已经看到“已请求”的重试。

**优先做最小可靠修复**：

- 恢复扫描必须识别 `retry_requested`，不依赖已经消失的 request 文件；以 request ID/版本判定，避免把历史已处理请求反复恢复。
- consume 不得在 durable claim/调度证据前永久 unlink 请求。采用先 claim 后 ack，或把持久状态本身作为可重放的唯一请求来源。
- 请求产生、接收进程领取、desktop 更新状态的交错必须有 CAS/锁定协议；单文件 atomic replace 不等于多文件事务。
- 同一 session 已在执行时，请求不能仅因 `_active` 去重而被丢掉。明确合并、等待或完成语义，并保存决定。
- 重复点击返回同一逻辑请求或清晰的现有状态。别让界面显示 retry_requested 后既不会恢复又不能再次操作。
- 状态文件损坏不能静默当“从未存在”；保留诊断及恢复依据。

如现有多文件协议无法简洁证明以上条件，使用 SQLite request/outbox 表，把 claim/状态变更放进事务；这是局部迁移，非搬迁全部 JSON store 的理由。

**验收崩溃点**：请求写入后、状态更新前；领取后、入队前；持久 queued 后、内存入队前；执行后、ack 前。每个点模拟重启，再重复执行恢复，最终要么完成一次，要么明确终止，不能永久等待或重复副作用。还需覆盖多个入口并发重试、已有 active session、磁盘写失败。

### ENG-04 — 错误分类与关联观测

**入口文件**：[desktop_server.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/interfaces/desktop_server.py)、[daily_events.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_events.py)、[daily_automation.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_automation.py)、automation state/retry 与 model runner。

**要求**：

- 未预期异常记录堆栈/exception type，并携带已有 request_id、session_id、job_id、attempt、date 等适用字段。对外仍用稳定、简洁错误；相同请求 ID 可以关联内部诊断。
- 区分来源冲突、非法输入、provider 可重试错误、预算终止、取消、存储故障、程序错误。不能把全部异常归为“稍后重试”。
- Daily 保持“失败不覆盖已发布内容”，同时记录失败阶段和根因。JSON read 区分 missing、corrupt、unavailable、mismatched identity。
- 暴露 worker running/stopped/failed、最近成功/失败与积压信息，不能只看 HTTP 端口可用判断系统健康。崩溃后采用有限恢复或明确失败状态，不建无限重启循环。
- 日志默认不输出录音原文、完整模型 prompt、凭据、原音路径；必要定位使用稳定 ID 和脱敏上下文。

**验收**：注入 HTTP 内部错误后，响应 ID 能关联一条完整内部记录；Daily provider 与编程错误可区分；预算 terminal 不触发无意义自动重试；worker 退出可见；损坏 JSON 有明确诊断；日志测试用敏感占位文本确认未泄露。

## 4. Phase 2：Daily 增量维护和查询成本

### ENG-05 — 唯一有界维护入口

**入口文件**：[bootstrap/core.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/bootstrap/core.py)、[mobile_sync.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/mobile_sync.py)、[daily_events.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_events.py)、[daily_automation.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_automation.py)、[daily_source_repository.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/daily_source_repository.py)、[v029_daily_dirty_dates.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/migrations/v029_daily_dirty_dates.py)。

**目标行为**：正常同步请求的工作量不依赖所有历史日期；提交来源变更时持久记录受影响范围，返回同步响应；后台 owner 有界维护，最终反映变更。

**实施步骤**：

1. 从真实 composition root 移除“同步回调 → 全历史 refresh_daily_cache”旁路。同步只记录必要的持久 dirty 信息并唤醒 owner，不在请求线程中逐日计算。
2. 区分本地投影/失效维护与远程生成。即使模型功能关闭，本地变更维护仍要运行；不能简单把所有工作转给仅在 Codex enabled 时才创建的 coordinator。
3. dirty 范围与源写入保持事务一致；wake 只是通知，不是唯一证据。进程在提交后、notify 前退出，重启仍能发现工作。
4. 明确 dirty 项的消费者。若本地维护与远程调度分开，不能由一个消费者提前删除另一个尚未处理的 dirty 记录；使用单 owner 分派、独立消费进度或明确 ack 协议。
5. 批量去重受影响日期，设置每轮日期/行数或时间预算。执行中发生新修改时不能误删新 dirty；重复处理应幂等。
6. 保持当前日/已完成日的原有生成策略。历史变更可失效和更新本地投影，但新增远程历史重生成不得扩大既定授权范围；完整历史 backfill 仍为显式流程。
7. `full_reconcile` 保持显式诊断/修复入口。不得通过另一条启动扫描重新引入日常全历史工作。

**可见一致性**：同步 receipt 与变化 cursor 的可靠性保留。异步投影尚未完成时应清楚呈现 pending/stale，而不是把旧结果当作新结果，也不能为等待“马上刷新”重新阻塞 sync。

**验收**：使用真实 `compose_v3_core → MobileSyncService.synchronize` 路径，而非单独 mock coordinator。构造 7/90/365 天含 utterance、人物、任务的数据；仅修改一个旧日，断言同步不刷新所有日期，后台只处理受影响日及定义好的近期日。覆盖模型关闭、重启、漏 notify、处理期间再次变更、连续同步分页、空日期/删除/跨午夜来源。

### ENG-06 — 批量输入和状态规则

**入口文件**：[daily_event_inputs.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_event_inputs.py)、[daily_source_repository.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/daily_source_repository.py)、[daily_inventory.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/daily_inventory.py)、[daily_generation_queue.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/daily_generation_queue.py)。

**查询调整**：

- Daily 输入只读取所需 speaker/context 字段，不为获得 speaker tracks 拉整份 session detail。
- 批量读取 audio_ranges、人物 profile、task evidence，避免每 utterance/person 都查一次；保留日范围与有效状态过滤。
- 先记录 SQL 次数、返回行数、query plan，再决定索引。用新 migration 加必要索引，不随意加大量冗余索引。
- 输出按稳定顺序规范化，确保批量查询不会意外改变 input hash、event identity 或语义结果。

**状态调整**：

- 用显式 classification/reason_code 替代 `"Current day" in reason`。展示文案不能决定 queue 行为。
- 形成一份状态转移表：当前状态、触发条件、attempt 是否递增、是否允许自动重试、lease 要求、终止条件。observe/claim/recover/finish/retry 共用其约束。
- 区分没有来源、上游不完整、当前日待完成、输入变化、provider 故障、次数耗尽、成功和人工 backfill；不要用多个不受约束 bool 代替状态定义。
- 暂保留当前 Singapore 产品时区。集中声明约定与跨层测试；本次不顺带实现全球时区功能。

**验收**：新旧读取结果语义一致，固定 fixture 的 fingerprint 稳定；一天从少量到大量 utterance 时不再一条一句地增长查询数（允许合理分块）；365 天中单日变化不加载所有历史；非法转移拒绝、attempt/retry 上限不回退、旧 lease 完成拒绝、文案改动不改变行为。耗时用于观察，不能只用机器相关毫秒数作唯一门槛。

## 5. Phase 3：执行期限和长期运行

### ENG-07 — 原生推理可终止、可恢复

**入口文件**：[processing_worker.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/processing_worker.py)、[native_pipeline.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/models/native_pipeline.py)、[ports/processing.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/ports/processing.py)、[audio/tools.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/audio/tools.py)、automation lifecycle。

**目标**：进程存活、租约续期与模型取得进展是三个不同事实。任何底层调用挂起不能无限阻止取消、关闭和恢复。

**要求**：

1. 明确 stage deadline、progress deadline、cancel 与 shutdown 语义；根据录音规模/设备配置给出有依据的预算，不能照搬远程模型 180 秒限制。
2. 对不能合作取消的 GPU/本地模型调用，优先采用独占子进程或等效可靠隔离，能终止本任务拥有的进程树；纯线程 timeout 无法停止底层执行，不算完成。
3. timeout/cancel 后先停止执行资源，再发布终止状态、释放 lease。旧 owner 的迟到结果始终不能写入新 run。
4. 处理模型加载、音频准备、transcribe、diarize、产物写入、backend close 各阶段；不能只给最外层函数包一层计时。
5. shutdown 有界且可解释。超期工作保持可恢复的持久状态；不将未完成任务伪装成 succeeded，不为关闭而丢弃已承诺数据。
6. 保留现有双模型、speaker 时间轴、artifact schema、checkpoint 与用户取消语义，不在此任务改识别算法。

**验收**：使用可控挂起的自有测试子进程，验证模型加载/推理/音频阶段卡住后能 timeout；取消和服务关闭在定义的上限内返回；子进程不残留；旧 lease 写入被拒绝；重启恢复不重复提交产物；正常长任务持续进展不被不合理误杀。真实 GPU 长录音另列验证结果，不能用 fake 测试宣称设备已通过。

### ENG-08 — 采样 lease、积压和 worker 健康

**入口文件**：[annotation_samples.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/annotation_samples.py)、[annotation_sample_repository.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/annotation_sample_repository.py)、daily/automation worker lifecycle。

先用 fake clock/可控慢 embedding 验证超过当前 120 秒 lease 的行为。若任务因正常长计算反复过期，增加 owner-token 约束下的有界续期或按阶段重新领取；仅把固定 lease 调得非常大不能解决 hang。过期 owner 不得续租或发布新 owner 的结果。

显式暴露 queued/running/failed、最老等待时间、worker last success/error；按真实负载评估 admission/backpressure。不得为“限制队列”静默丢录音任务。

验收涵盖双 worker、慢 embedding、lease 丢失、源 revision 改变、模型身份改变、重启、次数耗尽、close 后不再新领取。继续保留 dataset role/purity 与 model identity 约束。

## 6. Phase 4：模块边界与普通技术债

### ENG-09 — 按能力收口，不机械拆文件

**第一批入口**：[people.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/people.py)、[people_prototypes.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/people_prototypes.py)、[people_repository.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/people_repository.py)、[daily_event_inputs.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/daily_event_inputs.py)。

**推荐拆分边界**：证据身份投影、人物身份决策、样本任务编排、用户 policy 与机器成熟度 CAS、审核查询/操作能力。每项显式接收必要依赖与输入，返回明确结果；不要依赖另一个 Mixin 暗中提供的属性。

- event/reminder/daily 共用公开的 evidence identity projection，保留不同用途的业务限制。身份确认、训练许可、产品 self 推断不能因提取公共函数而混成一个布尔值。
- `domain.dataset_roles` 只保留纯领域判断；取 role 的 SQL 下沉 adapter，由 port 提供事实。
- ingest 的 `uow.catalog.connection.execute` 改为业务能力方法，不把 connection 暴露在 port。
- Daily coordinator 的队列、inventory、dirty-range/锁依赖明确为端口；bootstrap 只做组装，减少读取私有 analyzer 字段。
- POST/GET dispatcher 按现有业务能力分组；统一解码/校验与错误映射，避免每个 handler 自己写一份业务规则。现有路由/响应保持兼容。
- 对 Mixin 逐个替换，不要求一次消灭全部继承；小型 cohesive repository mixin 可以保留。禁止新建万能 `common/utils` 承接所有迁出逻辑。
- 架构检查要验证真正的禁止依赖与 SQL 穿透，而非只看字符串/LOC；区分模型 adapter 的 `execute()` 与数据库调用，避免误报。

**验收**：与改动能力对应的原有行为测试通过；端口能够用最小替身独立测试；没有换路径后的旧旁路；人物 policy CAS、确认任务、纯度 gate 和设备审核语义不变。文件变短但依赖未减少，不算完成。

### ENG-10 — 前端部分失败与缓存容量

**入口文件**：[query.ts](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/all_day_recording_front/v3/core/query.ts)、[ReviewsView.vue](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/all_day_recording_front/v3/views/ReviewsView.vue)、[SessionDetailView.vue](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/all_day_recording_front/v3/views/SessionDetailView.vue)。

- 辅助候选请求失败时，保留已经成功加载的主列表，显示局部错误/重试；不能将“加载失败”表现为“没有候选”。缺少必需数据的操作应有明确可用性，不发送不完整决策。
- 为缓存定义 TTL 清理和容量上限；保留共享 in-flight、subscriber 取消、generation/version 防晚到响应覆盖机制。清理不能终止仍被其他页面订阅的请求。
- 新增/修订成功后的 cache invalidation 覆盖相关概览、timeline、speaker summary 和审核页面，避免切换再返回看到旧状态。
- 只有在改动相关功能时才提取 timeline/editor/playback composable；本任务不全面重写三个大 Vue 页面。

验收使用实际 Vue 行为测试，覆盖局部请求失败、重试、快速切换/卸载、请求晚到、编辑后返回、缓存淘汰和共用请求。不能仅检查源码含有某字符串。

### ENG-11 — 重算请求的明确语义

**入口文件**：[knowledge_support.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/knowledge_support.py)、[derivation_repository.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/adapters/sqlite/derivation_repository.py)、[event_extraction.py](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/event_extraction.py)、相关 desktop 查询/状态。

当前审计仅确认存在入队、查询和完成记账，未确认通用消费者。先重新追踪所有运行时入口，避免按“队列表存在”推断自动重算能力。

**默认最小行为**：保留重算账本作为可见的待处理/失效状态，说明需要什么动作；不得显示成已由后台自动修复。若已有手动重生成入口，必须能正确处理 stale event identity，不能被 `_known_event_ids` 永久排除，也不能重复创建等价 active 事件。

若现有明确产品要求自动重算，再补专用有界 worker：来源版本快照、预算、claim/lease、失败/次数终止、人工确认对象排除、幂等发布。范围限制为允许自动派生的对象，不能顺带扩大历史重放或修改确认任务。

验收：修订已有来源后，自动对象 stale 且待办可见；手动/允许的自动重生成有可达路径；旧 provenance 保留；重复触发不重复发布；确认任务保持 active 意图；关闭模型时仍能说明待处理原因。未实现自动 worker 时，文档/界面不得承诺自动完成。

## 7. 迁移、契约与回滚

每个阶段先填以下判定，再实施相关持久化变更：

| 问题 | 处理要求 |
|---|---|
| 只增加 Python 类型/内部结构？ | 默认不需要 migration，不为重构形式增加表 |
| 变更已有 JSON scope/receipt？ | 增加 format/version 与旧格式读法；明确未知/缺失字段行为 |
| 新增 DB 字段、状态或 outbox？ | 按当前最新版本追加迁移；不要假设执行时仍是 schema 29 |
| 影响 Phone wire DTO？ | 协调 schema/OpenAPI/fixture/release lock 与消费端；否则保持契约字节不变 |
| 旧程序无法读取新数据？ | 说明兼容部署顺序和回滚限制，不能声称直接 checkout 即可回滚 |
| 历史数据可能已经有错误？ | 先只读识别/报告；代码修复不等于历史数据已修复，不自动覆盖人工事实 |

至少验证空库升级、上一生产 schema 升级、重复启动、迁移异常回滚、旧 pending/receipt 可解释。保留 SQL checksum 机制；特殊 Python backfill 要有固定 fixture，避免修改函数后绕开迁移语义校验。

优先兼容性增加、分阶段切换；删除旧读路径前证明没有必要消费方。禁止把生产 DB 当作迁移试验场。

## 8. ENG-12：验证矩阵与执行命令

### 8.1 验证分层

1. 每个语义缺陷：先跑其失败复现，再跑对应修复测试。
2. 每项完成：运行关联应用/adapter/契约测试，不只跑新测试。
3. 每个阶段结束：后端全量 + Ruff；改到前端时加前端测试/类型检查/构建；影响契约时加双仓库核验。
4. 最终收尾：完整矩阵一次，保留日志。若失败修复后重跑，如实记录，不覆盖首次失败或套用旧报告通过数。
5. 新逻辑、失败或未解决风险才需要扩大复测；通过后不反复无目的运行同一套件。

现有审计基线为 856 collected / 855 passed / 1 skipped，新测试加入后数量应自然变化。唯一原有 skip 为 private local integration opt-in；它不是全部外部设备能力均通过的证明。

### 8.2 本地命令参考

以下为 PowerShell。每次正式验收使用新的输出目录，避免旧 receipt/cache 干扰实验。

```powershell
Set-Location 'C:\Users\32673\PycharmProjects\AllDayRecording-ASR'
$validationRoot = Join-Path (Get-Location) ('outputs\engineering-refactor-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $validationRoot | Out-Null

.\.venv\Scripts\python.exe -X utf8 -m pytest -q --basetemp "$validationRoot\pytest-temp" --junitxml "$validationRoot\pytest.xml"
.\.venv\Scripts\python.exe -m ruff check src tests tools
.\.venv\Scripts\python.exe -X utf8 tools/sync_v3_contracts.py --phone 'C:\Users\32673\DevEcoStudioProjects\AllDayRecording' --check

Push-Location 'C:\Users\32673\PycharmProjects\AllDayRecording-ASR\all_day_recording_front'
npm test
npx vue-tsc -p tsconfig.v3.json --noEmit
npx vite build --config vite.v3.config.ts --outDir "$validationRoot\frontend-dist"
Pop-Location
```

实际执行时每条命令独立保存 stdout/stderr 与退出码；不能用最后一条成功掩盖前面失败。必要时写仓库内统一 validation 脚本，在失败时退出，并允许有依据地选择关联测试。默认正式矩阵不得静默跳过项目检查。

Windows 沙箱曾导致 pytest 临时目录 WinError 5、Vite spawn EPERM。若再次发生，先区分执行环境和代码错误，在许可范围内换可用的本地运行方式；不得因此禁用失败测试。不要删除未经确认的临时目录或已存在数据。

### 8.3 必须升级为正式测试的审计复现

- R1：生成期间插入真实 correction，再返回旧 fake 结果。
- R2：相同 session、新 revision、首次调用后 30 分钟以上；同 job 重试上限仍成立。
- R3：真实 sync 入口的历史规模测试，不能只保留 refresh spy。
- R4：消费请求后、调度前的持久状态恢复。

原始脚本仅作参考，正式测试应放进 tests 并独立构造 fixture，**不能依赖被 Git 忽略的 outputs 文件存在**。

### 8.4 不可省略的条件性验证

| 类别 | 自动化可证明的内容 | 必须单独标明的边界 |
|---|---|---|
| 外部模型 | fake 请求/预算/receipt/取消与结构验证 | 真实服务可达性、质量与耗时 |
| GPU/音频 | 自有挂起进程、lease fencing、终止协议 | 实机模型加载、显存回收和长录音 |
| Phone/Watch | 契约字节/fixture 与 host 逻辑 | 后台生命周期、设备重连、系统 Calendar |
| 性能 | SQL 次数、读取范围、缓存容量、合成历史趋势 | 真实用户数据分布与多日运行 |

未运行项记录 NOT_RUN 及原因，不用“测试全通过”省略范围。

## 9. 状态记录与最终交付

后续实施直接维护本文第 2 节及第 10 节状态；在现有 `PROJECT_ENGINEERING_REFACTOR_PROGRESS.md` 中追加补修记录，保留首轮命令、日志和历史报告。复核记录保存问题发现时的证据，修复后追加关闭证据，不抹去原发现。

每个任务记录：

```text
任务：ENG-xx
状态：TODO / REOPENED / IN_PROGRESS / DONE / BLOCKED / DEFERRED
起止基线：commit 或工作区差异说明
对应问题与入口：
最终行为变化：
实际修改文件：
新增/修改的行为测试：
迁移与旧数据/receipt 策略：
执行命令、退出码、日志位置：
未运行边界与剩余风险：
与本指导不同的实现选择及理由：
```

最终交付应包含代码/测试、必要迁移、更新后的 CURRENT_ARCHITECTURE、本文任务状态和进度报告。文档写最终实际行为，不堆积未采用方案。若发现本指导与新证据不符，记录证据并调整方案，不机械执行已失效设计。

### 最终验收清单

- [x] ENG-01：保留已通过的输入/确认/恢复保护，补齐 FUP-01B 模型候选修改链路。
- [x] ENG-02：新输入可创建新执行，同次重试/重启不能重置预算。
- [x] ENG-03：保留崩溃恢复保护，并通过 FUP-02 迁移后新请求去重测试。
- [x] ENG-04：错误可关联、可分类，worker 失败可见，日志不泄露业务内容。
- [x] ENG-05：保留有界维护，并通过 FUP-03 失效同步和客户端消费测试。
- [x] ENG-06：Daily 输入无逐句 N+1，状态不靠展示文案判断，来源 hash 稳定。
- [x] ENG-07：本地模型挂起可终止，取消/关闭有界，旧 owner 不能迟到发布。
- [x] ENG-08：样本 lease 和 worker 健康经长期运行场景验证。
- [x] ENG-09：本次触及的能力依赖明确，关键跨层/private helper 旁路收口。
- [x] ENG-10：局部请求失败可见，缓存有容量策略且竞态保护仍成立。
- [x] ENG-11：重算账本的实际能力、入口和产品表达一致。
- [x] ENG-12：相关与全量测试记录完整，迁移/契约验证通过，未运行项明确。
- [x] FUP-01B 完成后重新核对用户确认/修改流程，保留原始证据、声纹/盲测隔离和现有 wire 语义。
- [x] FUP-01B 新失败案例、关联套件和完整矩阵通过，追加新的日志与实际结果；第二轮通过记录保留。
- [x] 没有因本次整改自动执行未纳入范围的生产历史修复或远程回放。

**执行标准：关闭真实缺陷，建立可验证的边界，保留稳定设计。不得以文件变短、测试数字变大或文档写“完成”代替上述验收。**

## 10. 独立复核后的补修指导（第二轮）

本节保留第二轮补修的行为要求与已完成成果；FUP-02/03 维持 DONE，FUP-01 因第 11 节的修改入口遗漏重新打开。实际文件、测试函数映射、恢复入口、失败过程及历史验收见进度记录的“独立复核与第二轮补修”部分。只读检查和显式恢复能力已验收，生产历史修复未执行。

### 10.1 补修清单与交付方式

| ID | 原任务 | 优先级 | 目标 | 状态 |
|---|---|---|---|---|
| FUP-01 | ENG-01 | P1 | 独立提醒不会因其他候选确认而误判冲突，并有可追溯恢复入口 | DONE（FUP-01B 已关闭） |
| FUP-02 | ENG-03 | P1 | legacy 导入与新重试隔离，一次请求只调度一次 | DONE |
| FUP-03 | ENG-05 | P1 | Daily stale 状态原子发布到同步日志，并被消费端正确应用 | DONE |

每项先在当前未修复代码上增加正式失败测试，再实现修复，再运行关联套件。测试失败必须是预期行为断言，不是 fixture、import 或环境错误。优先扩展现有测试文件，测试独立造数，不读取 outputs 中的历史数据库。每项分别记录变更与验收，便于单独审阅和回退代码；持久数据回退仍遵守第 7 节。

### 10.2 FUP-01：区分规则候选的实际证据与模型上下文

**入口**：`application/reminder_extraction.py` 的 extract_product/_request、`application/generation_inputs.py`、`application/reminder_commands.py` 的 confirm/modify、`application/knowledge_generation.py` 的 submit_generation/accept_proposal。源文件均位于本仓库 `src/allday_asr/v3/`。

**已证实的触发条件**：同会话两条不同的 self 录音语句分别生成未来提醒，来源没有变化；确认第一条增加 active_reminders，导致第二条整份 reminder 快照不匹配。第二条被记为 conflict，随后 product_source_key 回放继续返回该终态。

**实施步骤**：

1. 分别列出规则提取和模型提取真正使用的输入。为产品规则候选建立可识别的快照 kind/格式版本，保存其实际读取的 utterance ID、revision、文本、有效状态、用于 self 判定的身份信息、录制时间、会话可用性与时区。完整保留会影响规则结果的字段；不要盲目只比较 revision，也不要复制整份会话作为替代。
2. 在提取的同一只读快照中捕获这些值。产品规则没有使用的活动提醒、其他独立语句不加入该候选的来源比较。需要防重复的业务规则另由候选幂等键、事件身份和写事务约束保证，不能靠无关上下文冲突间接阻止重复。
3. submit/accept/modify 均使用明确的快照分派器验证。校验与发布继续位于同一写事务；不得通过刷新旧快照、关闭 validate_snapshot、把旧候选自动绑定到当前来源来使测试通过。
4. 单独覆盖模型一次输出多条提醒的情况。模型实际读取过的活动提醒上下文必须保留校验。若允许同批输出顺序应用，必须以已持久化的 generation/proposal/operation 来源证明允许的变化仅来自该批输出，并保留目标事件 revision CAS；不能仅凭 session 相同或 actor 相同就放行。若发现两个输出存在真实依赖冲突，应显式进入重新验证/重新生成路径，不伪装为独立候选。
5. 明确旧格式 pending 的处理。没有可信规则输入证明的旧快照不得自动改写为新格式；保留旧校验，提供显式重新提取/复核入口，并解释原因。新旧快照的读法与拒绝未知版本行为都要测试。
6. 处理已经误判的 conflict 与幂等记录：先提供只读识别，列出候选、旧 generation、来源版本、冲突原因及恢复资格。恢复通过明确的用户操作创建可追溯的新尝试/替代候选，保留旧记录与关联，定义旧 product_source_key 后续如何解析；禁止删除幂等记录后盲目重跑。

**恢复边界**：没有足够历史证据时报告“需重新核对”，不得把全部 conflict 当成此缺陷。confirmed、modified、ignored、completed、cancelled 等用户决定保持原样。实现和验证恢复能力不等于自动执行生产历史修复；历史处理范围及实际执行结果必须单列。

**正式测试矩阵**：

| 编号 | 场景 | 断言 |
|---|---|---|
| F01-A | 两条独立产品候选，依次确认 | 两条 confirmed、两个不同日程，第二次无 source conflict |
| F01-B | 反序确认、并发确认不同候选 | 两条均可完成；同一候选并发/重放不产生第二个日程 |
| F01-C | 首条确认后重启再确认第二条 | 第二条仍可确认，恢复不依赖进程内白名单 |
| F01-D | 接受前实际证据变化、非 self、来源不可用、时区变化 | 旧结果不能发布，原已确认任务不被自动覆盖 |
| F01-E | 未使用的另一条语句或无关提醒变化 | 产品规则候选不误报来源冲突 |
| F01-F | 同批模型候选、外部上下文修改、同目标相互冲突 | 区分可证明的同批变化与真实冲突，不能全局忽略 active_reminders |
| F01-G | 旧 pending、已误判 conflict 的显式恢复 | 原历史保留；重复恢复幂等；无可信证据时拒绝自动转正 |
| F01-H | 忽略、完成、取消、人工修改后的重新提取 | 保留决定，不重新制造候选/日程/Calendar 操作 |

**关联测试**：`tests/test_product_reminder_loop.py`、`tests/test_v33_intelligent_reminders.py`、`tests/test_engineering_refactor.py`、V32 knowledge/semantic event 测试及 `tests/test_v3_device_sync.py`。必须走真实提取、确认和同步入口，不仅对字典 hash 做单测。

### 10.3 FUP-02：legacy 导入只解释旧数据

**入口**：`adapters/transfer/automation_state.py` 的 initialize/request_retry/consume_retry_requests/save，`automation_retry.py` 的 poll/recover/queue，以及它们的实际调用入口。

**已证实的触发条件**：旧 needs_attention JSON 已迁入 SQLite 但保留文件。新 request_retry 创建 UUID 请求并更新 SQLite 为 retry_requested；下一次 initialize 依据这个实时状态又创建 legacy 请求。

**实施步骤**：

1. 把 legacy 导入和正常请求流分离。导入只解释原始旧状态及原始 request 文件，不从迁移后的 workflow_states 当前状态构造 legacy ID。SQLite 继续是迁移后的权威，保留 JSON 仅供诊断。
2. 在 sidecar 中持久记录已导入的旧状态/旧请求身份或迁移完成范围；选择能覆盖部分导入、请求文件存在但状态缺失等兼容情况的标记粒度。标记、状态导入和请求导入原子提交，事务失败后可以完整重试。
3. 跨进程初始化、请求提交和调度依靠 SQLite 事务/约束协调。已存在 SQLite 新状态不能被旧 JSON 覆盖；重复 initialize/statuses/consume 无权制造新请求或更新新请求的身份。
4. 保留 consume 可重放读取、queued 与 ACK 同事务、active 会话等待而不丢请求的协议。旧请求已经 scheduled 或执行完成后，重复导入不重新变为 pending。不要把所有请求统一按 session 去重，否则下一次合法人工重试也会被吞掉。
5. 如需新增 sidecar 表/字段，定义 sidecar 自己的升级版本与事务策略；不要为它盲目递增主业务库 schema。升级时仍需协调停止/升级 receiver 与 desktop，避免旧 JSON 写入端继续工作。
6. 单独识别缺陷期间可能已存在的 UUID/legacy 双请求。只读报告需展示来源、请求身份、调度/执行关联和状态。只有能够证明属于同一逻辑请求且未被另一 owner 执行时，才可以在有审计记录的事务内合并或终止冗余项。不能仅按相同 session/接近时间批量删除；已执行副作用另行报告，不伪造回滚。

**正式测试矩阵**：

| 编号 | 场景 | 断言 |
|---|---|---|
| F02-A | 旧 needs_attention JSON → 迁移 → 一次新 retry | 一个待处理 request，保留旧文件不影响结果 |
| F02-B | 反复 initialize/statuses/consume 与进程重启 | request 身份、数量不变 |
| F02-C | 多个入口并发提交及并发初始化 | 同次逻辑请求唯一，无 lost update；验证跨进程边界 |
| F02-D | 旧 retry_requested 有/无 request 文件、重复导入 | 恢复一个请求，不能漏掉旧请求或再次激活 scheduled 请求 |
| F02-E | 首轮执行结束后再次 poll；之后再次合法人工 retry | 旧请求不再调度，新请求能正常创建 |
| F02-F | 导入标记前后异常、事务磁盘故障 | 全部回滚或全部提交，恢复后恰好一条请求 |
| F02-G | 已产生双请求的临时库及无法证明重复的两次请求 | 只合并可证明且符合状态前置条件的冗余项，保留合法请求 |

**验收重点**：不能停留在“pending 数量为 1”。用真实 poll/queue/recover 走完一次执行，清除 active 后再 poll，验证没有第二次排队、manual_retry_count 没有额外增加，原有发布幂等断言仍成立。扩展 `tests/test_engineering_refactor.py`，并运行既有 automatic workflow/receiver 相关套件。

### 10.4 FUP-03：Daily 失效与同步投影原子提交

**入口**：`application/daily_events.py` 的 mark_daily_stale、`daily_automation.py`、`daily_structured_summary.py` 的投影发布、`mobile_sync.py`，以及 `adapters/sqlite/operational_repositories.py` 的 change log。Phone 端仅按实际消费链定位是否需要修改。

**已证实的触发条件**：已发布摘要的来源删除/失效，后台本地维护插入 invalidation；DB 查询结果 stale，但最新 daily_summary 同步 payload 仍 active，新增同步事件数为 0。

**实施步骤**：

1. 建立共享的 daily_summary 同步投影构造入口，让正常生成与失效发布使用相同字段约束，保持 objective 的既有裁剪规则。不要把数据库内部记录或完整 events 列表直接作为 wire payload。
2. 在同一 UOW 中读取当前摘要、添加失效记录、读取有效状态并追加变化。发布失败必须回滚失效写入；coordinator 不得 ACK 尚未成功发布的 dirty 范围。内容、provenance 和生成预算保持原状。
3. 用当前有效状态和最近发布的同步投影判定是否需要追加。相同摘要已发布等价 stale 时不重复追加；已有 invalidation 但日志漏发时仍应补发。不能只根据 add_invalidation 的返回值决定发送，否则旧遗漏永远无法补齐。
4. 明确内容 revision、投影状态和 sync sequence 的关系。检查 Phone 的实际 upsert/缓存条件：是否拒绝相同内容 revision 的新状态、是否按 sequence 去重。优先沿用现有协议表达状态更新；若需修改消费逻辑，补消费端测试；若确需协议变化，协调契约及兼容部署，不能随意自增内容 revision 冒充重新生成。
5. 保持 snapshot watermark 语义。失效前已经开始的 bootstrap 可以返回该 watermark 的旧快照，但其后增量必须交付 stale；失效事务提交后开始的 bootstrap 必须直接看到最新投影。不得改写不可变历史 payload 来“修正”旧分页结果。
6. 处理已漏发的历史 stale：提供按明确日期/摘要范围的只读差异检查与有界本地补发能力，复用同一发布逻辑并记录修复数量。普通启动不扫描全历史，不调用远程生成；明确已有 dirty 已 ACK 的日期如何被显式修复覆盖。

**正式测试矩阵**：

| 编号 | 场景 | 断言 |
|---|---|---|
| F03-A | 持有旧 cursor → 真实来源删除/失效 → 本地维护 → 增量同步 | 接收到同一摘要的 stale 状态，正文保留 |
| F03-B | 失效提交后新设备 bootstrap | 最新投影 stale，不返回旧 active 作为当前结果 |
| F03-C | 失效前开始 bootstrap，分页期间失效 | watermark 快照自洽，结束后增量收敛为 stale |
| F03-D | 模型关闭、旧历史日期、重复 catch_up | 无远程请求，等价失效只发布一次，不阻塞 sync |
| F03-E | 插入 invalidation 后、append 前注入失败 | 事务无半提交，dirty 不丢失，重启补做成功 |
| F03-F | 新摘要生成与失效竞争 | 校验当前内容版本，不将旧 revision 的 stale 误用于新内容 |
| F03-G | 实际消费端同内容 revision 的新状态、重复/旧序列重放 | 正确显示 stale，不被旧 active 回包覆盖 |
| F03-H | 已有 invalidation 但日志 active 的旧库 | 指定范围补发一次；再次补发为零；不用重新调用模型 |

**关联测试**：`tests/test_daily_event_layer.py`、`tests/test_daily_automation.py`、`tests/test_v3_device_sync.py` 与实际 Phone 消费模块测试。数据库直接构造可用于故障 fixture，但至少一条测试必须走真实删除/修改入口 → coordinator → synchronize；仅检查 invalidation 表不算通过。服务端协议测试与真实设备验证分开记录。

### 10.5 补修验收与状态关闭

1. F01-A、F02-A、F03-A 的新正式测试先证明当前缺陷；关联测试编号应映射到实际测试函数，不要求一行表格机械对应一个文件或一个测试。
2. 每项修复后先跑相关套件。全部补修完成，再调用已有统一脚本运行完整矩阵，使用全新的输出目录：

```powershell
$followupOutput = 'outputs/engineering-followup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
.\.venv\Scripts\python.exe -X utf8 tools/validate_engineering_refactor.py --phone 'C:\Users\32673\DevEcoStudioProjects\AllDayRecording' --output $followupOutput
```

脚本会创建目录，不要预先创建同名目录。如果修改了 Phone 消费代码，补充相应仓库要求的检查；ASR 统一脚本的 contracts 检查不能替代 Phone 消费行为测试或设备构建。

3. 在进度记录追加三项的实际改动、旧格式处理、恢复入口、命令、退出码、失败/修复过程和 NOT_RUN。895/1 是本轮补修前的独立复跑基线，不预填未来通过数；不覆盖旧日志或历史 manifest。
4. 只有对应行为、兼容数据、恢复路径与关联检查通过，FUP 才置 DONE，并恢复其 ENG 状态。全部补修验收完成后更新最终清单。未进行生产历史修复时明确写“已提供恢复能力，历史处理未执行”，不能写“历史已修好”。
5. 本轮不扩大远程模型权限、不重置执行预算、不绕过用户确认、不引入全历史自动维护。修复不得以减少必要来源校验或牺牲不可变历史为代价。


## 11. FUP-01B：模型同批候选的修改链路收尾（当前执行入口）

| 收尾项 | 关联任务 | 优先级 | 当前状态 |
|---|---|---|---|
| FUP-01B | FUP-01 / ENG-01 | P2 | DONE，B01～B08 与六项完整矩阵通过 |

### 11.1 触发条件、证据与范围

同一次模型生成产生两个独立 CREATE_TASK 候选 A、B；用户先确认 A，来源和外部上下文均未修改。直接确认 B 可成功，但通过桌面 modify 修改 B 的标题并确认，会抛出 GenerationSourceConflict，最终只存在 A 的日程。产品规则候选的对应编辑场景已通过，不需要重新设计该路径。

复核定位：[reminder_commands.py:189](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/src/allday_asr/v3/application/reminder_commands.py:189)。独立合成复现结果：[result.json](C:/Users/32673/PycharmProjects/AllDayRecording-ASR/outputs/engineering-followup-review-model-edit/result.json)。源码行号会随实现变化，后续以 modify/submit_generation/accept_proposal 调用链定位。

| 相同前置状态下的 B 操作 | 实际结果 | 日程数 |
|---|---|---|
| 直接 confirm | confirmed | 2 |
| modify 标题后确认 | GenerationSourceConflict | 1 |

正式测试优先扩展 `tests/test_product_reminder_loop.py` 中的 BatchReminderGenerator 与模型批次测试；在独立 fixture 中构造相同场景，不依赖 outputs 的数据库或 JSON。先证明新测试在当前代码上因上述断言失败。

### 11.2 根因与三个必须连通的校验环节

直接接受 proposal 已把 generation_id 传给 validate_snapshot，用持久化证据识别同批已接受且未修改的输出。modify 在最初校验时未传这一身份；即使只补这个参数，新替代 generation 的提交与接受仍可能使用无身份或替代 generation 自身的身份，无法证明新增提醒来自原模型批次。因此修复必须覆盖整条链路。

| 环节 | 实现入口 | 必须保证 |
|---|---|---|
| 修改前读取/验证 | reminder_commands.modify | 从原候选、proposal、generation 解析原模型批次，校验原实际输入和允许的同批变化 |
| 替代候选提交 | reminder_commands.submit_generation → knowledge_queries._resolve_inputs | 替代 generation 保留自己的新身份，同时持久关联原候选/批次；在提交写事务重新验证，不仅依赖前置读取 |
| 最终接受与发布 | knowledge_generation.accept_proposal + finalize_edit | 在同一 UOW 再验证可信来源关联、原候选仍待处理、来源及目标 CAS，然后发布替代操作和同步投影、将原候选标记 modified |

### 11.3 实施要求

1. **区分新操作身份和来源身份。** 人工编辑产生的 generation/operation 仍是新的人工操作，不冒充原模型输出。保留原始 input_snapshot，另外通过已有候选关联或必要的版本化元数据表达来源批次；优先复用明确的 revises_candidate_id 关系，避免复制一份会漂移的“当前快照”。
2. **关联由服务端验证。** 根据持久化 candidate → proposal → generation 验证会话、来源证据、原候选状态及原快照的对应关系。任何来源批次字段都不能只因出现在 input_scope 就被信任；提交方不能指定任意其他 generation 来扩大豁免范围。关系缺失、不一致、指向其他会话或形成循环时明确拒绝。
3. **复用严格的同批证明。** 只有已经通过当前 `_batch_created_reminder` 等价条件证明的原批次输出，才可在来源比较中识别为本批产生的变化。保留 accepted proposal、candidate、CREATE operation、未修改事件/日程与 revision 的关联检查；不要忽略所有 active_reminders，也不能仅凭同 session、同 actor 放行。
4. **保留真实冲突。** 外部修改原批次已确认任务、改期/取消/完成、额外外部提醒、原始语句修订、身份或会话可用性变化，仍按实际读取的上下文阻止旧输出发布。已有目标操作的 expected_revision CAS 保留。待编辑候选的 title/location/scheduled_at 继续由现有人工修改规则校验。
5. **覆盖事务间隙。** 在前置读取后、替代提交后、最终接受前发生变更时，最终写事务必须拒绝失效发布；不能留下半完成的 modified 状态、额外 active 任务或同步事件。若采用两阶段候选提交，失败后允许保留可追溯但未接受的替代候选，必须明确其重试/清理语义，不把它当作已完成任务。
6. **保持重放与并发约束。** 修改与确认并发、重复修改请求、关闭后恢复都不能重复发布任务。使用既有候选状态和持久化关联解决，不引入进程内豁免白名单；原用户决定和历史 generation/operation 不改写。
7. **核对 Phone 入口差异。** Phone reminder.review edit 在同步事务内已有校验，桌面修复后应与其具有相同的来源保护和业务结果。只在实际需要时改 Phone/协议；不要为了形式统一扩大本次改动。

**旧数据处理**：本次复现止于修改前校验，不能据此认定旧候选已被持久标成 conflict，也不需要批量迁移所有提醒。旧 pending 或已存在的替代候选，只有能从持久化关系证明其来源时才沿新规则验证；无法证明时保持明确拒绝/重新核对。保留已确认、忽略、完成、取消及人工修改决定，不自动执行生产恢复。

### 11.4 正式回归测试与关闭标准

| 编号 | 场景 | 必须断言 |
|---|---|---|
| B01 | 模型生成 A/B，确认 A，再修改 B 标题并确认 | B 原候选 modified、替代候选 confirmed；两个日程，B 使用修改后的标题 |
| B02 | 反序、确认首条后重启、修改合法时间/location | 独立修改成功，不依赖内存状态；时间/字段非法输入仍拒绝 |
| B03 | 已确认 A 被外部改期/取消，或实际来源被修订 | B 修改不能绕过真实上下文/来源冲突；A 用户决定不被回滚 |
| B04 | 读取后、替代提交后注入真实 correction 或原候选确认 | 不发布失效替代任务；无半完成 modified 状态/重复同步副作用 |
| B05 | 并发确认与修改同一候选、重复请求、重启重试 | 最多一个生效决定；历史与幂等行为可解释，不重复创建日程 |
| B06 | 错配/伪造来源批次、其他会话候选、缺失关系或循环 | 显式拒绝，不能获得其他批次的校验豁免 |
| B07 | 模型同批直接确认、产品规则编辑、旧格式与未知版本 | 原已通过路径不回归，可信旧关联可解释，无可信证明不自动补当前来源 |
| B08 | Phone review edit 与桌面 edit 的同一合成业务场景 | 相同有效输入都能完成，真实来源变化都被拒绝；既有同步 receipt/Calendar 语义保持 |

B01 必须先在未修复代码上出现预期业务失败，再在修复后通过。通过真实 modify → submit → accept，不以单测 validate_snapshot 或 mock 成功替代；可用确定性回调/barrier 注入事务间变化，不靠 sleep 碰竞态。

先运行产品/模型提醒、knowledge 来源校验及 device sync 相关套件；完成后用第 10.5 节统一脚本运行一次完整矩阵，使用新目录，例如 `outputs/engineering-fup01b-<时间>`。若修改了实际 Phone 消费代码，再运行相应消费/构建检查；无改动时不要把历史设备结果写成这次新执行。

关闭时在进度记录追加 B01 失败证据、实现选择、B01～B08 实际测试映射、命令/退出码/日志和 NOT_RUN。FUP-01B 全部要求满足后置 DONE，再恢复 FUP-01 / ENG-01 并勾选最终清单。当前 950/1 和源码 manifest 是收尾前基线，不预填未来通过数，不覆盖旧日志。本轮已完成代码修复、正式失败复现与完整验收，实际映射、失败过程和新证据见进度记录的 FUP-01B 关闭记录；未执行生产历史修复。
