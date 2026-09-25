# 手机片段试听与人物标注

## 日常使用

优先打开「人物 → 去审核」，先看重要提醒、人物记忆、重跑后归属歧义和重复出现的未知声音。不必认识所有声音，也不必清空审核箱。全量分页、搜索和跨页选择保留在「高级整理」。

试听实际需要确认的原句，必要时播放前后各 3 秒；只勾选确认过的片段。默认不全选大组。人物归属与声音分类分开保存：听不清内容也能确认人物。分组混入其他人时仅选择同一人的句子，其余保持未知。已有样本审核继续支持确认、不是此人和不确定，逐个样本处理。

手机先把意图写入持久化 outbox，再覆盖本地显示。人物名单有本地缓存，新建人物也先本地保存。界面区分「本地试听」和「需连接电脑试听」；没有本地原音时，不能承诺离线可完成审核。刷新、重建投影不删除 outbox。样本试听目前仍从电脑获取，并非完整离线样本包。

保存后「待同步」只表示本地意图。同步成功表示当前事实已保存；声纹候选、采纳以及模型训练是三件不同的事。本次没有训练模型。合格候选在既有审核箱显示，每个人首批最多展示 3 条人工选择样本；并非要求逐句建库。低质量、听不清、混声或排除来源不得自动宣称已经学习。

## 2026-09-24 真实代码审计与定向修正

以下是代码与本地工程测试结论，不是真机或真实识别率结论。工作开始时两个仓库已有大量未提交改动；本轮保留并增量修改，没有重置工作区、清空数据或改动真实声纹库。

| 链路 | 已确认实现与本轮修正 | 关键位置 |
| --- | --- | --- |
| 本地持久化、投影、名单 | 复用 outbox、annotation_people、待同步覆盖层；保存和同步分开 | 手机 PhoneV3UseCases.ets、PhoneProjectionRepository.ets、PhoneV3LocalAnnotation.ets |
| 同步 | 原始请求摘要和回执仍持久化；同 operation_id 不改 payload。不同字段仅在完整修订历史可证明安全时合并；同字段必须显式引用本设备已成功的前序操作 | application/mobile_sync.py::_merge_annotation_revision |
| 大批量局部冲突 | 手机新操作拆为逐片段队列项，在本地事务中入队；原有服务器原子整组接口不变。相同毫秒入队按 rowid 保持顺序 | PhoneV3UseCases.ets::enqueueAnnotationItems |
| 当前人物事实 | 原实现通过 manual track + cluster link 保存稳定 Person 引用，而非只改名字；本轮补 person_annotation，含人物、来源、操作者、原音 media_id 范围、旧版本；不再强迫恢复正常发言 | application/speaker_annotation.py |
| 普通标注到样本 | 已证实旧断点：人工轨道已有 membership，analysis_inputs 跳过，普通标注没有补建候选。本轮允许未建样本的人工轨道进入分析，复用其分组，标注事务提交后触发本地候选生成 | people_clusters.py::process_annotation_samples、people_identity.py::analyze、people_repository_analysis.py |
| 候选到后续匹配 | 继续复用逐样本 review、人物质量策略、版本兼容、person_vectors 和分层匹配。生成候选不等于采纳；声音改归属使旧代表证据不再合格，并为新轨道补建候选 | people_prototypes.py、people_repository_prototypes.py、people_sample_eligibility.py |
| 重跑 | 新发言在相同会话中精确匹配原音范围且人物唯一、旧关联仍有效时恢复；拆分合并、不同归属或丢失关联则保留 annotation_review 并进入审核箱，不静默套用 | evidence_projection_repository.py::_restore_annotation、processing_service.py |
| 下游 | 复用 dependent_closure、失效事件和重算请求；提醒原有 tombstone 会让人工确认记录从手机消失，本轮改为 stale upsert，已送达状态不被失效改写。自动提醒仅能自动创建，修改/完成仍需既有审核 | knowledge_support.py::cascade_derivations、reminder_repository.py、reminder_queries.py |
| 撤销 | 新修订保存 previous_values 快照。电脑分类保存后可撤销本次，按当前版本整批检查、恢复逐条原有分类，不把所有内容强制改为 speech | processing_correction.py::undo_annotations、SegmentClassification.vue |

## 用途与兼容

- speech：普通发言，允许作为候选输入，最终质量仍需检查；来源未知不证明实际互动。
- live_speech：明确确认是现场实际对话，可作为互动证据；样本仍需通过质量与采纳规则。
- unintelligible：不信任文字，仍可独立确认人物，不自动用作样本。
- overlapping_speech：明确内容清楚但多人重叠，可提取内容，不作为声纹候选。
- remote_speech：电话或会议中的真实互动者，可提取内容；本轮保守不将远程音频作为声纹候选。
- media_speech：确定是电视/视频播放，不将其当成真实互动或承诺。
- background_speech：保留旧「背景人声／电视声」含义，来源未知，不能迁移成确定电视。
- non_speech：非人声/噪声，排除内容和样本用途。

统一 Python 用途规则在 domain/sound_kind.py；内容、声纹与明确互动 SQL 规则在 adapters/sqlite/sound_eligibility.py。语音互动统计和关系观察输入要求 actual_interaction=true 或明确的现场/远程互动分类，单独确认人物不会计成真实互动。人物归属不由这些用途规则清除。事件、提醒的责任人仍由原有上下文提取，不因确认说话人而直接替换任务执行人。

## 查询真实状态

已配对设备的 POST /device/v3/annotations 保留 people、audio、assign；新增 status：

```json
{"action":"status","utterance_ids":["实际发言 ID"]}
```

返回当前修订、人工归属、内容与样本用途、已有候选/采纳历史、source_usable、review_decision、模型与版本。source_usable 只代表原证据仍适用，还需检查采纳、人物策略和模型兼容才能参与匹配。没有样本记录不能被解释成已采纳。模型或音频失败保留已保存事实，记录 annotation.samples_pending 审计，可用现有会话分析入口重试。

电脑撤销接口：POST /api/v3/annotation-undo，body 为 selections: [{utterance_id, revision}]，使用现有会话认证。新 API 状态查询尚未全部接入手机保存反馈卡；手机仍以本地待同步及既有审核结果为主。

## 迁移、恢复与撤销

本轮人工事实和撤销快照复用 evidence_json / correction_operations，不新增数据库表，不删除原始转写、原音、人物或历史声纹。旧缺省分类仍为 speech。旧 manual track 的人物关联在重跑恢复时兼容读取；没有可靠快照的旧标注不能承诺一键还原，保留原始字段并人工纠正。旧模型整组关联不能冒充逐片段人工确认。

修正人物或分类后，旧样本保留历史但匹配读取排除不再适用的证据；撤销恢复旧轨道及分类后，原合格样本可重新适用。撤销前版本变化会拒绝整批，避免覆盖其他修改。手机目前可追加纠正，精确批次撤销入口在电脑；不能把「恢复 speech」当成恢复任意旧分类。

发布锁已对齐工作区已有迁移：电脑 schema 12、手机 schema 8；本轮未新增 schema 13。升级前备份数据库（SQLite backup 或停服务后完整复制含 WAL 的数据）和原音目录；恢复时保留服务版本与备份数据库配对。应用回退不能删除未知 evidence 字段，也不能删除待同步队列来规避不兼容。建议电脑、手机一起更新；本轮未安装手机应用、未重启生产服务、未执行真实数据迁移。

## 验证边界与仍待完成

工程测试覆盖：人物确认独立于听不清、同版本分类后关联、自设备同字段依赖、原始 payload 幂等、候选生成及采纳后匹配查询、改归属旧样本失效及新候选、精确原音恢复/拆分歧义、批量分类撤销、提醒过期投影保留。手机主机测试覆盖离线队列重建和依赖记录；不是设备断电与真实网络测试。

以下不应宣称已经完成：

1. 本人仍使用既有独立注册/校准库。普通人物标注新增的候选没有自动写入本人库，不能声称本人后续识别已经改善。
2. 无任何 ASR 发言的会话，现有 confirmed_enrollment_input 仍依赖成功证据发言，尚无安全的手机原音窗口注册入口。
3. 原音恢复只覆盖同会话、完全相同媒体范围；跨会话重组、拆分合并进入审核，不自动推断。重新分组后的人工轨道投影依然复用现有 link 基础设施。
4. 现有 quality_score 主要由有效片段时长构造，不是完备音质/重叠检测。已知重叠和排除类型拒绝建库；未检出的混声仍依赖试听。没有增加未经验证的通用声学阈值。
5. 手机没有完整离线审核音频包；需电脑试听的片段已明确标注。未验证真机帧率、长录音播放、实际断网重启及后台同步。
6. 样本生成在本地同步/标注完成后的调用中运行；模型首次加载可能延长响应。失败可重试，但未引入新的后台调度器或假称队列处理完成。
7. 本轮未合并已入队同字段操作，采用显式依赖顺序执行，避免在可能发送后修改相同 operation_id 的请求。

真实数据核验：state/evaluations 中扫描到 10 个 JSONL，73 条非空 reference_speaker、72 条 reference_identity，未发现 conversation_id，也未发现 reference_person_id。它们不能直接证明跨日期固定人物身份改善。评估准备与结果模板见 [人工真值与评测指南](evaluation-guide.md)。不上传原音或声纹，不用 fixture 指标替代真实效果。


## 本轮工程验收记录

- 电脑全部 test_v3*.py 加 test_annotation_purposes.py：173 passed（27.65 s），含实际互动与用途分离回归。
- 手机/手表既有主机测试：128/128（entry 67、phone 61）；包含 ArkTS 编译。存在原有设备能力/API 版本告警，未把主机结果当成真机可用性证明。
- 网页 npm test：4/4；npm run build：vue-tsc 和 Vite 均通过，已更新本地打包资源。
- 针对本轮核心 Python 修改运行 Ruff，通过。
- 未运行：真实音频前后效果对照、真机性能、手机断电/重启与真实断网组合验收。没有部署、上传录音或训练模型。

剩余实现缺口与验证缺口分列在上文，不能将本轮工程通过解释为整个任务最终目标已经验收。

## 第一阶段修复（R1 / R2 / R3 / R9，2026-09-24）

本节只记录本轮修改。两个仓库开始时都有大量未提交工作；本轮未 reset、清库或提交。起点已按文件字节、SHA-256、Git HEAD 和完整 status 保存在 `state/phase1-repair-baseline/manifest.json`。审核包中的补丁相对于该起点生成，不是相对于 HEAD。没有在可访问的两仓库及已提供附件目录中找到 REVIEW.md、source-evidence.md、REPRODUCE.md、test_independent_audit.py 或其日志，因此**未运行原始独立审核测试**。本轮以合成映射、固定向量写了同等业务断言；真实保存、修订、过滤、同步和调度代码均参与测试。

### 结果与修改位置

| 问题 | 本轮确认的根因 | 实现与行为证据 |
| --- | --- | --- |
| R1 | 每次 utterance revision 都级联失效；mark_stale 不区分人工确认和自动候选。手机正确地只调度 scheduled，所以服务器误标会传导成取消。 | `processing_correction.py` 比较文字、有效人物/身份和内容用途；`knowledge_support.py` 对不影响任务含义的变化保留事件。`reminder_repository.py` 保留人工确认调度，来源矛盾进入原有 invalidation/recompute 流程，返回 `source_review_required`；提醒页显示提示。真实手机调度源码未放宽 stale 规则。 |
| R2 | 样本检查依赖旧 utterance 行，纠正新投影后旧行仍是 A；原音窗口样本甚至没有 utterance 指针。 | `annotation_fact_repository.py` 在同一事务内修订事实并记录窗口级样本撤权；`people_sample_eligibility.py` 在实际 person_vectors/cluster_vectors 路径同时检查撤权、来源候选和当前原音事实。任何一个贡献窗口被纠正就撤回整个旧向量；其他不相关 A 样本仍可用。不会将 A 的向量改名为 B。 |
| R3 | 旧恢复逻辑只找人物人工标注，声音分类只作为人物恢复的附带字段。 | `evidence_projection_repository.py` 改为调用按维度恢复的事实仓库；独立声音事实无需 person_id、活跃人物分组或旧 utterance_id。用途规则同时用于 Python 提取和 SQL 内容/互动/声纹查询，歧义分类不成为普通可用内容。 |
| R9 | 历史 A 和恢复后纠正的 B 都被当作独立有效确认；恢复没有持久身份及前后关系。 | 新事实有稳定 fact_id、分离的音频窗口、维度、值、来源行/修订、actor 和显式 supersession。恢复只投影同一 fact_id；A→恢复→B→再重跑只应用有效 B。无前后依赖的 A/B 两个头保留 conflict，进入现有标注审核。 |

R1 用例修复前失败；R2 用例中旧 A 向量仍在实际查询结果内；R3 两种分类均丢失；R9 等价用例再次返回旧 A。这五个关键断言在修改前均失败，在修改后均通过。原审核包关于 R9 的具体 ambiguous_audio_mapping 断言未运行；本轮另有真实并发 A/B 待审与明确纠正后无待审的正反测试。

### 当前事实、历史与冲突

- 原音锚点来自现有 capture_segments/audio_assets 映射，每个 media_id/start_ms/end_ms 单独保存。不能把同一 media_id 的整段文件或不连续窗口的包围区间当成确认范围。
- `annotation_facts` 的 person 与 sound 是两个维度；`annotation_fact_audio` 保存窗口，`annotation_supersessions` 保存明确前驱/后继，`annotation_sample_revocations` 保存不可逆的旧人物样本撤权。事实内容及关系有不可改写/删除约束，只有有效状态可以变化。
- active 是有效头；superseded 保留历史但不参与恢复；revoked 是人物归属撤销后的有效空值；conflict 是仍并存的相反判断。恢复不创建事实。一个事实多次恢复不会增加独立人工确认数。
- 纠正以用户所见 utterance revision 中的 fact_id 为前驱。相反判断没有明确前驱就并存；不会用时间戳、最大 revision 或 fact_id 大小选胜者。对已被替代的旧投影提交，即使原 utterance 仍存在，也必须刷新后再处理。
- 明确处理审核冲突时，所见的冲突头作为前驱一并替代；未见过的并发头不会被抹掉。人物纠正不替代声音事实，反之亦然。
- 老投影保留原文和旧人工事实，并标记 annotation_outdated，不继续成为确定的模型输入；当前事实、历史失效标记、样本撤权、关联事件/记忆失效和变化日志在同一事务里提交。后续 embedding/重算失败不会恢复错误样本资格。
- 逐句桌面入口、手机离线同步入口以及受影响的现有人物分组关联/撤销入口使用相同修订原则。operation_id 原请求和 digest 仍保留，沿用原来的幂等与不同 payload 拒绝规则。

支持范围：人物在同会话原音窗口精确等价时恢复，即使旧 track 已不活跃也保留人工事实；人物拆分、合并或跨会话 track 不可直接复用时进入审核。声音可恢复完全覆盖且判断一致的精确范围、拆分和合并；改正拆分范围时保留原事实未覆盖部分的残余窗口及修订关系。不同声音值混合、只覆盖一部分、映射缺失会进入审核并阻止该受影响发言用于内容/样本。不会扩大到同文件的其他不相交范围或整次会话。任意复杂音频对齐仍未实现。

### 提醒语义和生命周期

非语义判断基于结果字段，而不是操作名：文字相同、有效人物/本人身份相同、内容是否可用于任务提取相同，才不使任务事件失效。例如 speech→live_speech 只补充互动来源；仍可更新互动/声纹派生物，不取消确认任务。

文字、有效人物/身份或内容可用性变化属于需要重新检查的来源变化。例如 live_speech→media_speech/non_speech 会拒绝旧的未确认提醒候选并记录 source_semantics_changed；重新提取仍可生成新候选。已确认任务保留标题、到期时间、生命周期和调度，只增加来源重新检查记录，并在现有提醒页提示重新提取、核对修改候选。自动生成不能覆盖用户编辑，已有人工任务的证据也不能再次自动创建另一任务；相关新建议留待确认。

用户确认的改期、取消和完成继续走现有事件操作，终态不被来源重算复活。人物分组的自动引用重绑也不能绕过已确认提醒保护。真实 stale 自动调度仍不保留。**历史 stale 提醒没有被批量恢复**：必须逐项证明只有非语义来源更新导致 stale、人工确认内容/时间未变化、当前事件仍有效、没有后续取消/完成/删除、没有更晚调度或用户操作，才能提出恢复建议；证据不足保持待核对。本轮没有对真实历史提醒执行恢复。

### schema 12→13、验证和回退

`migrations/v013_annotation_facts.py` 及 `annotation_fact_migration*.py` 由现有 migration runner 在 BEGIN IMMEDIATE 事务中执行，schema 升为 13；契约 release-lock/manifest 和手机 source receipt 同步更新，手机投影 schema 不变。

迁移读取人工人物标注、带可靠 actor/来源的旧人工分组、分类修订和可验证的恢复来源。人物来源元数据给出的 source_utterance_id/source_revision 去重；同一行的明确纠正链及 previous_values 可建立替代关系。声音恢复副本只作为原事实投影；只有创建时间接近而无关联证据时，绝不编造替代顺序。原分类 background_speech 原样保留。无法证明当前判断一致的范围保留冲突；缺失原音映射保留事实并阻止静默当作普通语音恢复。

迁移还撤回与有效归属冲突的历史样本资格、发布新的投影修订，并按已有依赖机制失效受影响旧事件/记忆；已确认提醒继续受 R1 保护。历史原音、原文、样本和人工操作记录不删除。重复初始化不会再执行已登记迁移；重复 backfill 不新增已导入事实、重复撤权或投影修订。

升级建议：先停写并通过 SQLite backup API 取得一致副本，在副本上运行正常 initialize、外键检查和本节回归，再安排真实升级。本轮只在自动生成的测试库执行。运行中任何异常回滚 schema、事实、撤权和投影；修正故障后可以重试。若升级后需要版本回退，停写并整体恢复升级前一致备份及匹配的服务版本；不要直接删除 13 的版本记录、表或关系，也不要让旧服务继续写 13 库。本轮测试中的模拟 12 库构造仅限一次性合成库。

### 实际验证与边界

执行命令、修复前失败日志、修复后日志、起点/终点文件哈希及两仓库补丁在本轮审核包中。核心定向回归位于：

- `tests/test_phase1_human_facts.py`：五个复现、真实手机调度链、独立维度、拆分/部分覆盖、并发、迟到请求、事务回滚、原音窗口与混合样本。
- `tests/test_phase1_migration.py`：旧数据、明确替代/真实冲突、声音分类、迁移失败回滚和重复执行。
- `tests/test_phase1_guards.py`：未确认候选失效、人工编辑保护、取消/完成不复活、实际提取/互动/样本输入、历史依赖级联、原分组入口兼容。
- 原有 v3 标注、同步、提醒、人物记忆、洞察、投影、架构和契约回归一并执行。原提醒测试中“已确认任务遇来源矛盾就 stale”的旧预期已改为验证保留调度并记录来源待审，没有删除该测试。

手机逻辑测试用 `tools/quality/check-phase1-reminder.mjs` 对现有 ArkTS 源码作 TypeScript 转译，执行真实 applySyncResponse、DTO 解码、PhoneV3UseCases 与 PhoneV3ReminderScheduler，只替换存储、时钟、系统通知接口。它消费服务器真实同步响应，验证无取消、无重复发布，另验证 stale 仍会取消。另运行 DevEco/Hvigor 主机回归。**未验证真机通知，也未验证真实音频效果；不宣称声纹准确率提高。**

第一阶段仅覆盖 R1/R2/R3/R9。第二阶段仍包括逐句提交与样本聚合、同步人物分组和 500 项队列排空、声音恢复后补候选、模型升级重建、本人库、完整离线音频包及审核主页面等；本轮未修复、未全局 skip，也未将其宣称解决。由于原审核包缺失，无法列出原包第二阶段测试的实际失败数量。


## 2026-09-24 第二阶段 R4—R8：本轮实施及复验

本节为第二阶段交付；上面的第一阶段“尚未解决”列表是当时状态，不代表本节结果。此次原审核包已提供，原文件保留在审核包 `original-audit/`，没有把旧快照作为当前实现。两仓库真实起点为带未提交修改的工作区，开始前保存 372 个服务器文件、188 个手机文件的内容与 SHA-256。交付 `diffs/` 仅比较本轮快照与结束状态，不把所有 HEAD 差异归为本轮；新增文件明确列出。

### 根因、修正和证据

| 项目 | 当前代码根因 | 本轮修正 | 行为测试 |
| --- | --- | --- | --- |
| R4 | `analysis_inputs` 按每次标注产生的 manual track 裁剪；三次提交各得到 3 秒输入 | `annotation_sample_plan.py` 从 v13 有效人物事实按 person_id、session_id 汇集原音；与同步操作和临时轨道解耦；普通 ASR 分析不再另建逐句人工样本 | 原审核 mobile/grouped；`test_phase2_samples.py` grouped/split/incremental、重叠、不连续范围 |
| R5 | PhoneV3Utterance 重建没有读取权威人物 ID；声音类型还被用作人物分组键 | `PhoneV3PersonFact.ets` 独立校验 person 维度的 outdated/review/state；模型构造恢复稳定 ID；声音标签独立；未知不按显示名合并 | 原 grouping probe；真实服务器响应→PhoneProjectionRepository SQLite→loadCached→真实页面 speakerKey |
| R6 | 推送只取首批 500；循环仅由 has_more 控制 | 每轮重读可发送队列，推送/拉取独立；同字段前驱等待，冲突分支不阻挡无关操作；成功回执先到时持久保留覆盖层，直到对应权威 revision 已落库 | 1001=500/500/1、跨批依赖、部分冲突、多页、断线、无进展、取消、并发；闭环中注入本地写入故障，验证事务回滚和真实服务器幂等重放 |
| R7 | 声音恢复没有处理触发；失败只有日志，没有可恢复任务 | schema14 的会话待处理记录由事实/投影事务内 SQLite trigger 写入；桌面、手机、分组纠正/撤销共用；服务独立 worker 处理 | 原 sound restore；模型失败事实仍保存、重启、有限重试、speech→排除→speech、A→B→A |
| R8 | “已有任意 prototype”阻止再分析，包括撤权与旧模型 | 幂等键包含人物、会话、有效授权 fact IDs、规范化窗口、模型/版本、预处理版本；模型变化刷新正常队列；发布前重新读取事实和版本 | 原 v1→v2；迟到计算、同事实重跑不新增确认/样本、无关文字 revision、不活跃旧轨道 |

`test_phase2_preflight.py` 额外验证：分组 A→B 在已有 confirmed scheduled 提醒时成功提交，旧样本退出，任务内容/时间/调度保持且来源待审；A→B→撤销 A 保留旧撤权记录并生成新候选。后一风险修复前确实失败，并非把风险描述直接当作结论。

### 聚合、资格与任务规则

第一版只在同一人物、同一会话、有可靠 capture/media 映射的来源内聚合，不跨会话或日期混库。先取当前 active 人物事实，任何重叠的相反/撤销/冲突人物头或声音冲突、用途排除均阻止对应窗口。声音用途还检查已有投影中的重叠、媒体、人工样本排除等规则。历史 superseded 投影不重新覆盖当前事实。缺失映射或原音明确显示原因，可恢复后重试。

窗口按 media_id 和时间排序，重叠和连续范围求并集，不连续范围绝不跨空隙拼接。最多选 5 个 0.8–8 秒实际窗口（最多 40 秒）；不足 0.8 秒的尾部不作为独立裁剪。每个窗口都经现有裁剪、16 kHz 解码、CAM++ embedding 和归一化聚合路径；发布前要求 provider 返回的 representatives 覆盖所选窗口。质量分沿用现有的时长/12 秒上限 1，未调低人物 0.50 默认门槛。这是工程一致性，不是完整声学质量评估。

一个人物/会话/模型版本只展示一个当前聚合候选；证据增加会形成新候选并将旧聚合候选移出当前候选集合，历史保留。已有正确采纳样本不会仅因新增证据而撤权。改变任一贡献事实、排除贡献窗口或撤销授权后，旧混合向量通过真实 `usable_voice_sample` 查询立即失去资格；历史撤权不可删除。无关窗口的正确采纳样本仍保留。新的人工聚合样本经历声音排除后，恢复 speech 也要重新处理及审核。第一阶段的旧匿名聚类分类仍遵循既有可逆用途规则，未把匿名聚类历史一律永久撤回。

事实不是 track 或候选。重跑是原 fact 的投影；幂等键不含 utterance_id、operation_id、track_id 或任意 revision。旧载体轨道不活跃时可为新候选创建现有格式的审核载体，不新增人工确认。模型 v1、已撤权向量和审核历史保留；v2 必须生成新候选，不能继承旧 review。正常当前模型重匹配不消费旧模型候选。

任务只保留每会话一个合并意图，记录 generation、状态、尝试数、到期重试和带 token 的租约。事实事务只写意图，embedding 在事务之外。桌面和接收服务启动 worker，服务结束停止；重启自动读取未完成任务及模型变化。每轮有界领取，租约 120 秒、失败自动重试间隔 30 秒且最多 3 次，之后明确保持可手动重试；失联 worker 租约也可回收。重复/并发结果以 token、当前授权键和模型再次核对，唯一键防止重复落库。不可用/排除/冲突不会无限重试。人工“重试样本处理”只重新入队，不再确认人物。

实际匹配还要求 active 来源、无历史撤权、已采纳、人为审核仍确认、人物链接有效、known 人物、相同模型及版本、达到当前人物样本质量策略。auto_match_enabled 决定后续自动身份决策，不等于样本不可参与建议。本人仍使用独立校准库。

### 手机同步和最小反馈

每次同步默认 500 项、最多 100 轮；每轮重新查询尚未尝试且前驱未阻塞的操作。一次调用中缺少回执的操作保留到下次，不高频重发；网络错误保留原 payload；依赖/冲突保留，不重写旧 operation_id。拉取多页与本地排空各自判断。取消在当前网络请求完成后生效；同一 repository 的同步互斥；无游标进展或预算耗尽有明确退出原因。

schema9 的 outbox.applied_revision 保存“服务器已接受、投影尚未到达”的中间状态。这些项不重发，但仍保留本地人物覆盖，收到权威 revision 后才与投影在同一事务清理。重启、分开到达的回执/投影、服务器已提交但本地写入失败均可恢复；不因临时清理覆盖层而拆组。尚未发送的新纠正继续覆盖迟到旧投影。

已有标注面板增加按当前页一次查询（最多 20 句，API 上限 100）的样本状态及重试；不逐行联网。显示本地待同步、服务器保存、冲突/失败，与待处理/处理中、证据不足或不适用、可重试失败、候选待审核、历史采纳但不可用、当前可匹配分别表达。查询结果只对应同 revision 的行。候选仍通过已有审核入口试听、采纳；没有新增训练入口或识别率承诺。同步结果显示完成/部分完成、剩余数及主要原因，取消入口在同步页。

### 升级、回退与已有数据

服务器 schema13→14 新增 `annotation_sample_queue`、`annotation_sample_sets`、`annotation_sample_runtime` 和三个待处理 trigger，不改写 v13 人工事实历史。迁移在现有 BEGIN IMMEDIATE 中，为已有事实所属会话回填正常待处理意图；不会清空短样本、旧模型、原音、转写或撤权。首次 worker 按当前模型重建合格聚合候选，之后重启/重试幂等。新生成候选仍需审核；旧采纳不转给新向量。

手机 schema8→9 只新增 applied_revision，保留 outbox、冲突、缓存人物和投影。公开投影格式仍为 4、契约版本 3.7.0；release-lock、manifest 哈希和手机 source receipt 对齐。原 schema13 的 checksum 未修改。

仅在合成库验证：迁移异常整个事务回滚、修复后重试、重复 initialize 不重复入队/创建、事实和历史样本原样保留；手机实际 SQLite 8→9 保留未发队列。未操作真实生产数据库。真实升级前停写并通过 SQLite backup API 取得一致备份，在副本先复验；回退需停写后整体恢复对应版本的一致备份及程序，不能删除版本号或直接让旧程序写新库。已确认提醒的第一阶段保护不变，历史 stale 未批量恢复。

### 复验材料与未验证边界

审核包 README 记录实际命令、依赖和源码路径；validation 保留初次失败、修复中失败和最终结果，不用后来的通过覆盖早先日志。原审核装配仅增加保存后的真实 worker 推进与新手机状态读取接口，业务断言保留。裁剪测试首次在 Windows 沙箱因 WinError 5 失败，诊断及获准在合成目录重跑的日志均保留；真正修复前结果为原 Python 审核 6 通过/3 失败（R4 手机、R7、R8），两个手机探针失败；第一阶段 26 项通过。

闭环 `check-phase2-e2e.cjs` 使用 Node SQLite 的 native RdbStore 接口替身、Python 真实服务和单行网络替身；覆盖离线→关闭/重开数据库→分批推送/多页拉取→回执与投影事务失败恢复→稳定分组→实际窗口选择与聚合流程（裁剪和模型后端使用替身）→既有设备审核 API→实际 person_vectors→纠正一个贡献窗口→旧混合向量消失→已确认提醒经真实 PhoneV3ReminderScheduler 刷新不取消；还验证同名不同 ID 与真实并发事实冲突。小场景将查询批次容量限制为 2，正常 500/500/1 另有独立真实 SQLite 回归。

本轮不等于整个项目验收。本人独立库接入、完整离线音频包、无 ASR 原音注册、审核主页面重做、新模型训练、任意复杂对齐、真实音频识别收益与真机性能/通知仍未验证或不在范围。未声称声纹准确率提高。SDK 日志中的已有权限/API 可用性告警保留，主机编译与通知接口替身不替代真机验证。

最终工作区复验：服务器相关回归 224 项通过；手机 Host 128/128、ArkTS 编译通过；前端 4 项与构建通过；真实 SQLite 队列与完整闭环通过。最终日志与导出源码复验结果见审核包 validation。

## 第二阶段收尾 P2-01—P2-04（2026-09-24）

本节修正前节交付的交叉边界，不表示新增第三阶段功能。依据新提供的 phase2-independent-audit.zip；正文任务范围优先于附件中的建议。本轮起点保存于 `state/phase2-closeout-baseline/manifest.json`，含服务器 332、手机 174 个相关源码/清单文件的原始字节哈希、两仓库 HEAD/status。没有 AGENTS.md，没有 reset 或清真实库。本轮差异只对该文件级起点计算，原有 dirty 修改不算本轮贡献。

### 四项修复与证据

| 问题 | 根因及修改位置 | 修复后的业务行为 |
| --- | --- | --- |
| P2-01 | `prototype_candidate` 把 current=0 过滤后，服务和 `add_prototype_review` 都无法撤回。修改 `people_repository_prototypes.py`、`application/people_prototypes.py`，独立查询某人物的历史 confirmed 授权 | S1 被 S2 替代推荐后，S1 已采纳授权仍可撤回；实际 person_vectors 不再返回它，S2/其他正确采纳、事实、提醒保持。错误人物/引用拒绝；重复撤回返回明确“仅已确认可撤回”，不重复记历史。 |
| P2-02 | worker 将 key 存在当作当前计划完成。修改 `annotation_samples.py`、`annotation_sample_repository.py`、`people_sample_eligibility.py` | 重新核验当前计划、事实、来源、租约、generation 和模型后，选择有效历史结果为 current；选择与作业完成同一事务。待审 S1 可返回，confirmed/rejected/retracted 的原审核决定均保持，不复制授权、不重复 embedding。 |
| P2-03 | 时间最早五段锁死代表集合。修改 `annotation_sample_plan.py` | 可靠同人窗口先求重叠/连续并集，按最长时长、media_id、起止时间确定性选最多五段，再按原音顺序送 provider。五个 0.8 秒之后新增 5.5 秒时，选长段加四个短段，实际 8.7 秒，评分 8.7/12；没有使用未选音频或降低 0.50 门槛。 |
| P2-04 | max revision 被误用为每条发言目标，整组 overlay 无法退出。修改 `mobile_sync.py`、`device_sync.py`、`sync_repositories.py`、schema15，以及手机 `PhoneProjectionRepository`/`PhoneSyncProjection`/`PhoneV3LocalAnnotation`、schema10 | 新回执携带逐资源结果；每项权威 revision 或更新的 tombstone 到达即停止该项覆盖。其它未到达项继续保护；新的未发送纠正继续覆盖。旧多选升级残留通过原 ID/payload 幂等重放恢复，不重做原标注。 |

独立审核原三个 Python 测试未改业务断言，直接在项目 Python 3.12 完整依赖中运行：修复前 3 failed，修复后通过。原 `export_legacy_sync.py`/`probe_legacy_sync.cjs` 在副本运行，修复前可见 non_speech 遮住权威 speech、剩余 1；修复后可见 speech、剩余 0、complete。没有使用审核包的外部依赖导入隔离作为产品代码。

### 生命周期与代表选择规则

- current 只控制当前候选推荐，不是采纳授权。历史 confirmed 仍在桌面已有“查看已确认”列表；手机原审核页增加“已采纳样本授权”行，只提供单样本撤回。即使来源变得不适用，历史采纳仍可管理；撤回入口不要求重新试听或改变人物事实。
- 新增采纳继续要求当前候选、有效来源/事实、未撤权、known 人物、正确链接、当前模型/版本和当前人物质量策略；最终写入事务再检查候选和策略。历史撤回查询不能用于 confirmed。
- 当前计划命中历史计算缓存时，仅在来源授权仍有效时重新选择。pending 仍 pending；accepted 复用同一条已有采纳授权；rejected/retracted 保留决定，不能因自动 worker 变回 pending/accepted。真实来源撤权继续永久拦截；同一被撤权 key 不自动重生，新有效事实沿正常新 key 生成/审核规则处理。
- S1→S2→S1 可以反复发生。唯一 sample_key 防重复；复用不新增向量、审核、人工确认。发布前核验整个计划键集合、任务 token/generation、provider 版本；不符合则留 queued。事务故障回滚选择及作业完成，租约回收后重试。
- 选择仍限同人物/会话、最多 5 段、单段 0.8–8 秒、最多 40 秒。长连续范围从起点切成最多五个 8 秒候选块，不足 0.8 秒尾段不单独使用；全部候选按时长优先，再用 media_id/start/end 打破平局。不能跨间隙取包围区间。重叠只计一次，无效新片段不改变有效选择。
- 策略标识升为 `annotation-union-16k-v2-longest5x8s`，包含于幂等键和持久 runtime key。现有库服务启动后检测变化，将已有事实会话进入正常有界增量处理；无需重标或清样本。选择改变的新向量不继承审核；旧正确 accepted 不因增加证据一律撤权。选中窗口被改人/排除仍即时撤回受污染整体。
- 时长分数只是已有工程代理指标，不能表述为真实声学质量或准确率提升。

### 逐资源回执、旧数据和迁移

`OperationReceipt.resource_results` 为兼容可选数组，每项 `{resource_id, revision}`，旧 `resource_revision` 最大值保留给旧消费者。新的多选操作保存真实逐项结果，no-op 也记录实际未增加的 revision。手机校验 ID、正整数修订、重复 ID 和与原 selections 的完整对应，收据/投影/逐项完成标记/游标同一事务。

手机 schema9→10 新增 `outbox_resource_receipts(operation_id,resource_id,target_revision,projected)`，8→10 经过原8→9迁移。已接受单选可从原 scalar 回执可靠回填；已接受多选缺逐项结果则重新发送**原始请求**取得恢复结果，不修改 payload、operation_id 或改造成新操作。此重放也受同一批次/尝试/无进展预算限制。

服务器 schema14→15 仅新增 `annotation_receipt_recovery`。旧 `client_operations` 全行不可修改约束保持，连原 receipt_json 都不改：验证设备与原 payload digest 后，已接受旧请求的恢复分支只发布当前权威资源快照与各自 revision，或明确 tombstone；不再次执行原标注。恢复目标在旁表与变化日志同一事务保存，后续重放返回相同目标、不会重复发布快照。目标可能晚于原操作，是服务器核验的后续权威状态，不是用 base+1 或超过 base 猜测操作完成。已重跑的旧资源发布其当前 retired/superseded 状态；已不存在资源发布明确 tombstone。

每项已投影后停止该项 overlay，即使同组另一项还未到达；全部满足后清理 outbox。单项完成标记持久化，重启保持。更晚权威纠正不被旧操作遮住，新本地未发送操作仍有效。同步剩余数包括尚未收敛的接受项。若手机已升级但服务器仍旧版，没有可靠逐项结果时保留等待，不能伪造完成；两端升级后再次同步可恢复。协议版本仍3.7.0、投影4，增加可选回执字段并更新 manifest/source receipt；schema 为服务器15、手机10。

升级及回退仅在合成库验证：v14→15 故障完全回滚、重试/重复初始化；旧请求/回执字节不变；v8/v9→10 保留队列。真实部署前停写、SQLite backup 获取一致备份，在副本先验证后升级两端；回退整体恢复对应版本的库和程序，不删版本号或放宽不可变约束。未操作真实生产库、未批量恢复 stale 提醒。

### 实际验证和表述更正

服务器完整相关回归 **244 passed**，包含原两阶段 224 项和本轮 20 项（原审核3项、补充17项），不重复相加。手机 Host **128/128**、ArkTS 编译通过，既有 SDK API 告警保留。真实 SQLite 原1001项500/500/1、既有闭环和新增旧队列探针通过。初次扩大回归4项失败来自旧 DeviceReview 假仓库忽略 status 参数；只修正 fixture 按状态返回，没有删除原业务断言；失败日志保留。

新增 `test_phase2_closeout_guards.py` 覆盖：历史撤回与其它采纳隔离、事实/提醒不变、拒绝非法采纳、审核决定保留、多次往返、重启、模型变化、选择/完成事务回滚、成组/多种顺序、重叠/极短尾段/排除长段、迁移失败、旧回执幂等及已删除资源。`phase2_closeout_export.py` 生成真实服务响应；`check-phase2-closeout.cjs` 经过真实 SQLite/模型检查逐项覆盖退出、相邻新本地纠正、v9残留重放、no-op、事务失败、重启和 tombstone。

**纠正前轮表述**：原 `test_phase2_samples.audio` 及原跨端闭环 patch 了 extract_clip，写合成零 WAV；它们验证真实选段/聚合业务流程，不验证实际文件裁剪。此次另外添加 `test_actual_extract_clip_on_synthetic_wav`，不 patch extract_clip，用合成10秒分区波形运行真实 FFmpeg，核验三组窗口采样率、长度和每个样本值。这个独立范围测试通过，不改变原闭环的替身边界。

仍未验证真实录音识别收益、真机性能或系统通知；本人库、完整离线包、无ASR注册、审核首页重做、模型训练和阈值调低均不在本轮。附件独立审核的196项子集包含51项重点测试，未与其相加。本轮使用完整项目依赖，未全局skip或排除外部SDK测试。命令、各轮原始日志、导出源码复验和文件级diff详见审核包。

导出交付核验：审核包源码在独立目录加载，服务器同一244项再次全部通过；导出的两端源码完成旧队列兼容探针及原完整闭环。前端本轮npm test 4项通过（未修改前端源码，未重跑浏览器交互或网页构建）。仅复用既有Python/TypeScript运行时依赖，未借用原工作区业务模块。包内所有哈希、补丁相对起点应用及导出业务源码与已测文件一致性均验证。


## 聚合样本完整试听小修（2026-09-24）

本轮起点逐文件保存于 `state/review-audio-baseline`，含两个仓库原有未提交文件的字节和 SHA-256。原交付 ZIP 的 SHA-256 仍为 `71d43d0070662a53badd9187e3874a5f1236b9c4435b3a0dc9e488588811e99f`。本轮没有改动事实头、sample_key、选择策略、模型、阈值、审核授权或 schema，没有操作真实音频库。

C-AUDIO-1 原因是服务端仅裁剪 `representative_clips[0]`，手机及桌面把播放开始当作整样本完成。现在 `interfaces/review_audio.py` 从存储中的完整代表窗口生成有界计划；`device_reviews.py` 逐窗真实裁剪，再按原有顺序拼接 PCM WAV，不插入间隙。最多 5 窗、总时长 40 秒、每次内部裁剪不超过原 15 秒上限，最终字节仍限 16 MiB。重复、重叠、非法范围、超过边界和源文件截断均拒绝；不修正、拓宽或丢掉非法历史窗口来凑出可播放结果。临时裁剪文件在成功及失败后删除，手机缓存停止、完成、出错后删除。

响应保留原字段，追加 `complete_sample`、`audition_key`、`windows`（media_id/start_ms/end_ms/playback_start_ms）、`window_count`、`total_ms`。key 只绑定试听内容，不替代 sample_key 或采纳授权。手机请求每次绑定当前 review/prototype，桌面 `/api/v3/voice-audition` 每次绑定人物及合法候选/历史管理记录；客户端不能提交 media_id 或起止位置覆盖服务器窗口。桌面复用原认证、来源检查及会话恢复，CSP 仅为本机 Blob 音频增加 `media-src blob:`，未放行外部媒体。Windows 客户端中止连接按已有断连分支结束，不改业务状态。

手机 `PhoneV3ReviewAudioPlayer` 使用现有公共 AudioPlaybackService 的 AVPlayer 完成回调，删除原时长计时器；ViewModel 核对完整标志、内容 key、窗口数和总长后，只有全部播放完成才记录当前样本完成。桌面 `VoiceSampleAudition` 为 VoiceReviewActions、PeopleView 常规/可选训练/历史入口共用，使用同一个 HTMLAudioElement，只有 ended 解锁。界面显示所有原音范围、有效总长、正在播放范围；停止、失败、切换、刷新、页面退出/隐藏/后台不沿用完成状态。没有跳过试听伪装为完成的路径。记录的是播放器完成事件，不能证明用户认真听完。

实测发现 PeopleView 没有识别服务器已有的 human_selection，人工聚合被前端分流隐藏。仅补齐该标记的客户端类型与显示分流，使之与现有服务端 primary 分流一致；服务器采纳资格和匹配阈值不变。

C-AUDIO-2 原因是 accepted_grant 被音频模式白名单拒绝。现在允许当前历史授权管理项复听非 current 的原音，不要求其仍可用于匹配。缺文件时快照返回 audio_available=false 和原因，手机/桌面禁用试听，撤回按钮保持独立。错误人物/原型/失效审核引用拒绝；历史管理项不能 confirm，原有 confirmed/retracted 事务及匹配资格没有放宽。

兼容：新增 JSON 字段可选，无数据库升级或数据迁移；新版手机遇到旧电脑缺少完整标志/key 时不解锁确认，显示更新/刷新提示。旧手机自身的“开始即已听”行为必须升级客户端后才消失，因此两端应一起更新。离线缓存旧快照刷新后自然获得新描述；审核完成覆盖仅在内存，不迁移旧已听状态。回退本轮代码不需要回滚数据库，不批量调整任何历史授权。

验证：原独立音频审核 3 项未经削弱，修复前 3 failed，修复后通过；新增 12 项覆盖真实 FFmpeg 波形逐样本一致性、跨 media、40 秒/15 秒内部裁剪、0.8 秒单窗、非法/重复/重叠、缺文件、截断/清理、历史非 current 复听及独立撤回。完整相关服务器回归 259 项包含这些 15 项，不相加；第一阶段、P2-01—04、同步迁移继续通过。Host 129/129（entry 67、phone 62）；前端原 4 项、TypeScript/Vite 构建通过。真实手机 SQLite 的旧多选、500/500/1 和原跨端工程闭环通过。

桌面另以真实 Edge 执行生产 Vue 组件的开始/完成/停止、错误、换候选、重建与隐藏事件；还运行构建后的 PeopleView + 实际认证 HTTP + SQLite + FFmpeg，完成 9 秒串播、实际采纳、历史复听和不依赖试听的撤回。手机执行真实 ViewModel、审核播放器与公共播放器，只有原生媒体/文件接口被替身模拟。未做手机真机听音、通知或系统后台交互验收；独立全页面 CompileArkTS 任务在当前 SDK 未注册，保留尝试日志，不把 Host 编译说成全页面构建成功。未声称真实识别率提升。

所有失败、中间装配修正、环境失败、最终通过日志及精确命令见本轮审核包 README 和 validation；独立审核原附件也原样保存。原跨端样本闭环仍用合成零 WAV 替换裁剪；本轮新增音频测试和实际桌面闭环才使用真实 extract_clip + 合成波形，数值声纹模型是固定向量。
