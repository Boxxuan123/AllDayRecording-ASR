# 历史本人身份 Backfill 与手机本人审核箱（2026-10-02）

## Scope

本轮是产品数据修复。复用已有音频、capture 映射、自动 speaker turns 和当前 CAM++ 本人 matcher；未重新 ASR、转写、diarization、聚类、enrollment 或 calibration，也未调整阈值或放宽双窗条件。

开始时两仓库工作区均干净，并已 fetch origin：

- ASR：`590d0f8418f6b9f8daa2e6c70c2d5097139a7a24`。
- Phone：`fbb524aa446d2e92fa5d69dbaeb2c3520c133496`。

## Architecture

`HistoricalSelfBackfillService` 生成私有、带内容摘要的 dry-run plan。Apply 仅消费该 plan，逐条在同一事务重查人工事实、revision、query、音频文件签名和 policy/voiceprint 摘要。任何变化使整个事务拒绝；不会在 apply 时扩大 cohort 或重跑模型。同一 plan 再次 apply 返回已存结果，不增加 run、item、纠正或派生任务。

私有 schema 26 增加不可变 backfill item 和人审 ledger；分发契约仍使用现有 core contract schema 24。审核协议新增 `self_identity_review`，沿用现有手机快照、音频接口、持久审核队列和 operation ID。

身份语义：

- `utterances.identity` 是当前产品投影，`original_identity` 保留初始识别事实。
- Speaker track 是分组和自动 ownership 证据，不等于人物事实。
- 人工 identity correction、person annotation、已绑定人物具有优先权，回填不覆盖。
- Profile/prototype 是独立的声纹资产；本人审核不创建人物标注或声纹样本。
- 既有产品 self run 记录在 `speaker_cluster_runs`；历史回填使用独立产品 ledger，不回写原研究或产品 prediction。

身份变化通过既有 `apply_utterance_correction`，保留原始值、纠正历史、审计和手机 change log。相关 artifact/derived data 标记 stale/pending；不全库重算提醒、summary、关系或 memory。

## Safety / Isolation

自动 Self 使用共享 production gate：至少两个有效独立窗口，每窗至少 2 秒，所有窗口通过原 matcher。相同 SHA 的物理音频范围不能因重复 capture 冒充独立窗口。Foreign、overlap、非 exclusive ownership、映射不完整及缺失音频不产生 Auto Self 或审核候选。

Review 仅采用安全 query 中已有 matcher 的 Self 决策：有效窗口不足双窗规则，且没有相反 Non-self 窗口。没有新增候选阈值。主界面不展示 cosine 或阈值。

默认跳过 Blind/holdout/independent reservation、冻结 prediction、enrollment source、人工身份和具体人物绑定。被 sound/ownership/映射 gate 排除的行仅记录 KEEP_UNKNOWN 诊断，不进入 embedding 或身份修改。研究 truth 只用于独立指纹核验，未用于筛选身份或模型推理。

审核“是我/不是我”只写明确的人类 identity correction 和不可变人审记录；“不确定”保持身份并终止本轮推荐。三者都不会进入 person annotation → sample/profile 管线。迁移修复了原 SQL trigger 对 identity-only update 的无条件 sample enqueue；其它人物、文本、sound、证据和生命周期修改仍保留原学习失效语义。

## Dry-run metrics

最近 7 天：0 条 eligible Unknown；3 个冻结实验 session 跳过，其余可用录音已绑定人物。

按授权 fallback 到最近 100 个 eligible session；库中实际只有 26 个 session，其中 24 个非 tombstoned 被检查。没有执行不设范围的 full-history apply。

| 指标 | 数值 |
|---|---:|
| Eligible sessions | 14 |
| 潜在 eligible Unknown（包含 gate 拒绝诊断行） | 2,013 |
| Utterance 范围总时长 | 4,622,628 ms，约 77.04 分钟 |
| AUTO_SELF | 0 |
| REVIEW_SELF_CANDIDATE | 4 |
| KEEP_UNKNOWN | 2,009 |
| Unknown → Auto Self | 0% |
| Unknown → Review | 0.199% |
| Unknown → remain Unknown | 99.801% |

4 条审核片段均为安全单窗，时长 2.80–3.28 秒。比例是产品恢复/推荐数量比例，不是 recall 或 ground-truth accuracy。跳过的 5,031 行为已绑定人物的产品行，包含已有稳定身份，不能理解为 5,031 条 Unknown；另跳过 4 行人工 override。3 个冻结 session 在运行模型前整 session 排除。

### 当前 Unknown 原因

| 原因 | 数量 |
|---|---:|
| Foreign speaker / overlap | 760 |
| 安全长度不足 | 603 |
| 缺少 persisted ASR/diarization evidence | 220 |
| 当前 Self score 不足 | 178 |
| Exclusive ownership gate | 157 |
| Excluded sound | 88 |
| 窗口质量低于 production minimum | 3 |
| 仅一个有效 Self 窗口，进入 review | 4 |

220 行确实缺少 turn-aware query 所需的持久 ASR/diarization evidence；本轮未重建或猜测 ownership。当前 cohort 的 2,013 行都没有该产品 self 路径的持久 trace。这只能证明“未找到该路径记录”，不能据此声称从未运行过其它旧识别路径，或把每条 Unknown 归咎于旧 bug。

### 按天

| 日期 | Auto | Review | Keep |
|---|---:|---:|---:|
| 2026-08-24 | 0 | 0 | 219 |
| 2026-08-29 | 0 | 0 | 1 |
| 2026-08-30 | 0 | 0 | 127 |
| 2026-09-02 | 0 | 1 | 390 |
| 2026-09-03 | 0 | 0 | 155 |
| 2026-09-08 | 0 | 0 | 1 |
| 2026-09-09 | 0 | 0 | 698 |
| 2026-09-10 | 0 | 0 | 3 |
| 2026-09-11 | 0 | 0 | 47 |
| 2026-09-13 | 0 | 3 | 367 |
| 2026-09-22 | 0 | 0 | 1 |

### 按 session（匿名 SHA 前缀）

| 匿名 session | Auto | Review | Keep |
|---|---:|---:|---:|
| 0738a349ba5a | 0 | 0 | 1 |
| 418fe28b3cb9 | 0 | 0 | 1 |
| 4cd5b65916c9 | 0 | 0 | 3 |
| 559f6365d6dc | 0 | 0 | 1 |
| 6fd0a38d8d8f | 0 | 0 | 219 |
| 77a007cc7466 | 0 | 3 | 367 |
| 7bf25fc298b2 | 0 | 0 | 47 |
| 7dcfa60b576d | 0 | 0 | 37 |
| 89144492f494 | 0 | 0 | 14 |
| 8fbbdfc0f39f | 0 | 0 | 90 |
| 9b2aa7359f13 | 0 | 0 | 155 |
| a000d8883dae | 0 | 1 | 305 |
| c500475a89df | 0 | 0 | 85 |
| cf9b5493a172 | 0 | 0 | 684 |

### 按 duration bucket

| 时长 | Auto | Review | Keep |
|---|---:|---:|---:|
| 2-4s | 0 | 4 | 349 |
| 4-6s | 0 | 0 | 132 |
| 6s+ | 0 | 0 | 176 |
| <2s | 0 | 0 | 1352 |

## Apply metrics

消费完全相同的 quiet dry-run plan：恢复 Self **0**，创建本人审核任务 **4**，KEEP_UNKNOWN **2,009**。所有 item 包含 run/session/utterance/revision、前后 identity、query/window decisions、score/threshold、reason、算法版本与创建时间。私有 run ID：`e3bbae84d538bd811eab0016dee5bd84179808522773c7e21f587b8bcc007f9b`。

第二次 apply：全部数据库表和资产指纹与第一次 apply 后完全一致。公开报告不包含私人原句、人物姓名、原始录音路径或真实 utterance ID。

## Review workflow

现有审核箱的“声音”分类显示“可能是本人”，包含录音时间、原转写、时长、简化证据及完整试听。“是我/不是我”需要完整播放；结果先存手机 SQLite，离线及进程重启仍可重试同一 operation。已完成对象由权威快照移入 history/从待处理中消失；多端迟到提交返回既有结果，不能覆盖第一条事实。不确定状态抑制立即重推荐。

## Real-device verification

连接的 HarmonyOS 实体机已构建并安装最终正式 HAP。

- 真实 production candidate：实际打开审核箱，看到时间、转写、简化证据，完整播放；播放结束后确认按钮可用。没有代替用户提交真实身份判定。
- 隔离开发库的两个 **synthetic** item：完整播放并分别点击“是我”和“不是我”。PC 收到 1 个 confirm、1 个 reject，投影分别为 Self/Non-self；手机待处理项消失，再次同步不返回。
- 合成人审前后 annotation/learning/research/profile 表及 fixture enrollment NPZ、policy 指纹完全一致。正式库人审记录为 0，保留原 4 条候选待用户审核。
- 验收中修复了试听结束后 ArkTS 卡片按钮未刷新的问题，并重新编译安装、复验真实试听。
- 已恢复正式接收/网页服务，并从正式 PC 重建手机投影、恢复自动同步，清除合成测试缓存；手机原始录音和离线状态保留。

Synthetic confirm=1、reject=1 只验证交互和事务语义，属于 diagnostic only，不用于评价 review 候选准确率。实际用户人工确认样本数为 0。

## Blind / Independent integrity

首次 dry-run 时后台设备鉴权/游标/audit 有正常写入；研究表和资产仍完全一致。暂停已确认空闲的服务后，重新对同一有限 cohort dry-run：**全部数据库表及资产指纹完全一致**。

Apply 后及恢复服务后：Blind queries/predictions/truth/evaluations、holdout、research reservation/usage、learning exposure、annotation/sample、persons/profile/prototype、processing/speaker group 数据及 enrollment/reference/calibration/policy/model/original enrollment sources 全部保持原指纹。只有私有 schema migration 和新增 backfill ledger/item 发生预期变化。本批 Auto=0，因此产品 utterance 没有自动 identity 变动。第二次 apply 全状态一致。

## Tests

- 修改前 ASR 完整基线：516 passed、1 skipped、2 架构失败。
- 新增 31 个 backfill/review 测试实例，覆盖 strict Auto、单窗 review、短窗、低 score、foreign/overlap、capture 短尾/连续性、重复物理窗口、映射/音频/证据缺失、人工优先、人物绑定、enrollment、Blind/independent 隔离、JSON plan apply、事务竞态、幂等、音频、三按钮、多端重放和不学习。
- 最终完整 ASR：547 passed、1 skipped，仍只有原 2 项架构失败；违规文件列表与基线一致。两项分别是维护行数限制，以及 application/domain/ports 禁止 adapter import；没有顺手重构无关文件。
- 相关历史回填 + query coverage：41 passed；原 self identity regression 也通过。
- Phone 完整宿主：161/161。新增真实 SQLite/use-case 脚本验证同步/筛选、完整音频及 stale 拒绝、三种 action、离线重启、远端提交后本地失败的相同 operation 重放、已处理抑制；现有 durable review queue 回归通过。
- 最终手机 assembleHap 通过，HDC 安装与实体机验收通过。
- Ruff、compileall、py_compile、Git diff whitespace 和双仓库 contract receipt 检查通过。

## Known limitations / non-self → Self

本 cohort Auto=0，没有产生自动 non-self → Self；这不证明 matcher 的总体误接受率为零。已有人工 Non-self/具体人物绑定默认排除，本轮未把这些人工事实用于重新拟合或做新的真实 ground-truth 评估。隔离 synthetic reject、人工优先及多端测试证明纠正路径不会覆盖人工 Non-self。

大量 Unknown 属于长度、ownership/foreign 或缺失自动证据；这些不能通过降低阈值或放宽双窗来修复。暂未提供本人审核独立历史页面，已处理状态在权威 review snapshot 保留，待处理卡片会消失。不确定没有自动重排机制。

## Full-backfill recommendation

暂不建议直接做一次全历史 apply：这次最多 100 eligible session 已覆盖库中可筛选的旧数据，剩余主要是证据 gate 拒绝。应先由用户听 4 个候选，再单独审计缺失自动证据的最小可恢复范围。

只读全历史估计（完成有限 apply 后）：14 个 session、2,009 条潜在 eligible Unknown、4,610,628 ms（约 76.84 分钟）；4 个已呈现审核对象不会重复推荐。尚无超出本轮已扫描数据的明显新增收益。

后续正式命令（本轮未执行全量 apply）：

```powershell
allday-asr self-backfill --limit 100000 --dry-run --plan outputs/historical-self-backfill/full-plan.json
# 检查私有 plan 和完整性，再消费同一 plan：
allday-asr self-backfill --apply --plan outputs/historical-self-backfill/full-plan.json
```

也支持 `--days`、`--since`、`--until`、`--session-id`、`--state-dir`；dry-run 不迁移或写数据库，已有 plan 不被覆盖。

## Git

仅提交本轮服务、私有迁移、共享安全 gate、CLI、审核协议/UI、测试和匿名报告。两仓库正常提交并推送 origin/master，不使用 force。最终 40 位 commit 和 GitHub 链接随交付给出；local HEAD、origin/master 和 ls-remote 三者核对一致。所有真实 plan、指纹、数据库、音频、布局及详细日志位于 Git 忽略的 outputs/。
