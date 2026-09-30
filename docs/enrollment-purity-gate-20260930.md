# Enrollment purity gate 与 shadow 评估

本轮把人物事实与声纹画像资格拆开，新增原音范围级纯度证据、保守 enrollment gate、从原音重提 embedding 的 shadow profile，以及 identity-agnostic query purity probe。生产人物识别、生产画像、CAM++ 权重、G/P、margin 和 auto-confirm 均未改变，手机仓库未修改。

人物标注表示内容归属；纯度证据表示音频是否适合 enrollment。主要人物正确但背景混入另一说话人的片段，可以保留人物事实，同时被 clean gate 排除。

## 数据与迁移

Schema 22 是 additive migration，沿用现有 schema version/checksum、事务、WAL 和 FULL synchronous 机制。新增：

- `speaker_purity_sources`：按 media/start/end 唯一去重。
- `speaker_source_purity_evidence`：不可修改/删除的共享证据快照，保存 primary、purity、冲突、审核引用和完整 provenance。
- `speaker_purity_current`：当前证据引用；`speaker_purity_targets`：同一原音对不同 enrollment target 的独立关联。
- `purity_candidates`：候选及 recrop 状态，独立于人物事实、prototype authorization 和生产匹配。

审核引用保留 task、review ID/revision/source/time、audit run 和 target；坐标只在 source 层保存。只取同一 task 的最新 revision；不同 task 的矛盾答案显式标为 conflicting，禁止静默选择“最新的一条”。Undo/新 revision 会立即使旧 grant 失效，需要重新 reconciliation。原审核历史与人物事实不改动。

169 条审核全部迁移，无冲突、无 unresolved。全部审核范围中的 target-clean 为 102 条；其中 **87 条来自现有有效画像**，另外的 clean 查询/抽查证据不混入 enrollment。本轮使用这 87 条形成 clean pool。

只有 `clean_single AND reviewed primary == target AND verified source audio available` eligible。mixed、boundary、wrong-primary、uncertain 和 unreviewed 一律排除。Wrong-primary 只生成历史修正候选，不修改事实。Boundary 只输出待裁剪/复审清单；新的较小范围是独立 source，绝不继承 clean。

## Shadow profile 与覆盖

复用生产的原音读取、16 kHz 预处理、CAM++ provider 与 centroid 表示。缓存键包含原音内容 hash、range、模型/配置 hash 和预处理版本，缓存只来自本轮原音提取，不读取旧 aggregate embedding 来生成 clean profile。CAM++ 权重与冻结配置核对，提取前后验证模型文件未改变。

每人物/session 最多选最长五条已审核 clean 窗口，计算单位 session centroid，匹配取最高 session cosine。未引入新 weighting、multi-prototype 算法或 AS-Norm。共生成 18 个 session centroid。没有 mixed/boundary/wrong source 进入 shadow：**0**。

最低可用要求沿用 worker 的 0.8 秒窗口和 provider 的 duration quality：session duration 必须达到 `12000ms * person.minimum_quality`，默认 6 秒。至少两个独立 session 是本轮保守覆盖建议，不是经测试集调出来的阈值；单 session 标作 seed/insufficient，不 fallback 到污染源。日期使用 session 的 IANA timezone；非法历史缩写使用明确的客户端时区并记录 fallback。

| 去标识人物 | Legacy source | Clean pool | 排除 | Clean session | Clean date | 覆盖状态 |
|---|---:|---:|---:|---:|---:|---|
| A | 25 | 11 | 14 | 5 | 4 | 达到最低建议 |
| B | 74 | 50 | 24 | 6 | 4 | 达到最低建议 |
| C | 15 | 9 | 6 | 3 | 3 | 达到最低建议 |
| D | 14 | 11 | 3 | 2 | 2 | 达到最低建议 |
| E | 5 | 5 | 0 | 1 | 1 | 单 session，需补数据 |
| F | 1 | 1 | 0 | 1 | 1 | 单 session，需补数据 |

本人当前无有效 legacy enrollment source。人工确认的本人 query 只用于 query sensitivity，不能复用成 enrollment 后再评价同一 query。Clean pool 数量与实际最长五条/session 的 selected source 数量分别记录，不能混为一谈。

## Query probe

对原音片段做非重叠约 2 秒子窗口，计算 pairwise cosine、centroid distance、dispersion、leave-one-out、时间间隔和现有 track/cluster provenance。没有 VAD speech duration、overlap probability 或 diarization confidence 的地方保存 null，不伪造。子窗口只是提特征，不是重新授予 enrollment clean。

输出 PASS / SUSPICIOUS / INSUFFICIENT。少于两个窗口、range duration 未达冻结 6 秒或无法校准为 insufficient，不能算作 suspicious。PASS 只表示内部一致，不能代替人工 clean review，也不能验证主要人物身份。Wrong-primary 可能完全是单一说话人，理论上不能保证被此 probe 检出。

169 条 review 是 source-clip gold。对 exact range 可直接评估 clip 内一致性；组合 query 有明确 mixed/boundary 窗口时可证明含污染；只有所有 exact 窗口都被确认同一人物且 clean，才建立 clean query-level truth。其余保留 window-only/no query-level gold，映射清单存于私有输出。

采用 **leave-one-session/media-component-out**，共 13 个分组；共享原音的 session 合并，防止同源裁剪跨 fold 泄漏。预先选择 minimum pairwise cosine 为主要规则，比较其他单特征与简单组合，不按测试错误挑最优规则。每 fold 仅用其训练 clean 数据的分位数拟合 95% clean retention 目标。每个 historical identity query 也排除共享 session/media 的训练 component。没有读取未来 blind 音频或结果。

主要规则的 out-of-fold 结果：

| 指标 | 结果 |
|---|---|
| 全部 clean 的 PASS retention | 47/102，46.1% |
| Clean insufficient | 52/102，51.0% |
| 可评估 clean 的 retention | 47/50，94.0% |
| False suspicious | 3/102，2.9%；可评估 clean 中 6.0% |
| Mixed detection | 5/43，11.6%；可评估 mixed 中 5/29，17.2% |
| Boundary detection | 1/6，16.7%；可评估 boundary 中 1/3，33.3% |
| Wrong-primary detection | 0/8；7 个 insufficient，1 个 PASS |
| Uncertain handling | 10/10 insufficient；enrollment 全部排除 |

跨 session 有弱信号，但 short-clip 不足和持续重叠音色稳定限制检出。更关键的是，clip 内校准不能直接推广到跨片段 query：纯本人历史 query 仍被误判 suspicious。现有规则不适合生产 abstention。

## 冻结四组消融

A = legacy profile + legacy query；B = clean profile + legacy query；C = legacy profile + probe abstention；D = clean profile + probe abstention。C/D 只对 suspicious 自动 abstain，insufficient 保留 legacy decision。未修改 query centroid、matcher、split、G/P 或 margin。

以下是 G 的 **retrospective diagnostic only**；P 完整结果在私有 JSON。Source 与历史评估音频可能相关，query view 不是独立事件，不能视为生产准确率保证。

| Set | Arm | Known correct/total | Wrong known | Unknown FA/total | Self→other |
|---|---|---:|---:|---:|---:|
| Controlled | A | 8/16 | 4 | 7/21 | 1 |
| Controlled | B | 9/16 | 4 | 5/21 | 2 |
| Controlled | C | 4/16 | 2 | 6/21 | 1 |
| Controlled | D | 4/16 | 2 | 3/21 | 2 |
| Track | A | 5/7 | 0 | 5/17 | 4 |
| Track | B | 5/7 | 0 | 2/17 | 1 |
| Track | C | 0/7 | 0 | 1/17 | 1 |
| Track | D | 0/7 | 0 | 1/17 | 1 |

Enrollment cleaning 改善历史 track false accept，并保留 known recall。Query abstention 在 track 上把 recall 从 5/7 降到 0/7；两个路径的收益不能只看 A vs D。Controlled 下 clean profile 的 self→other 从 1 增到 2，是下一阶段必须关注的风险，不宣称所有混淆都已解决。

## 永久回归与根因

高风险历史事件保存为私有 integration fixture；公开测试只有去标识 synthetic 数据。一个原事件的三个相关 query view，legacy+G 保持 2/3 false accept，clean+G 降为 0/3；P 两种 profile 均拒绝。人工逐段确认的纯本人 query，两种 profile 的 G 均 0/3 accept。

Mixed query 的三个 view 被 probe 标为 suspicious；纯本人的三个 view 也被标为 suspicious。纯本人 query 的 mean pairwise cosine 更高、max centroid distance 更小，但还不足以支持可靠 PASS。没有针对人物或 session 特判、没有为这个事件改阈值。

Wrong-primary profile source 的 provenance 已沿 fact → cluster → sample set → source prototype → accepted copy → review 追溯。该短窗口被两代 aggregate 保留；首次 aggregate 的总 duration quality 已超过默认 0.5，后续达到 1.0；两次确认只验证 aggregate 和 cluster-person link，没有每窗口纯度约束。实际审核 primary 是另一位已知人物，不预设其为本人。

Worker 会合并相邻人物事实范围，优先选最长五个片段。Provider 平均各窗口 embedding，质量分只看总 duration；prototype confirmation/production person_vectors 依赖 human-confirmed、active grant、model 和 quality。没有 overlap 或跨 speaker 一致性 gate，因此“抽听主体人物后批量归属”的合理操作被误用为所有窗口的画像授权。这是约束缺失。

新增 worker shadow candidate 登记只处理已经选出的 enrollment source，不为所有 utterance 造任务。队列按原音与 target 去重，记录 coverage gap/new session，限制同 session 的候选数量。CLI 可导出候选、提出包含于原 range 内的 recrop；本轮未向手机审核箱批量投放新任务，未改 UI。

## 复现与验证

```powershell
python tools/speaker_purity_pipeline.py migrate
python tools/speaker_purity_pipeline.py build
python tools/speaker_purity_pipeline.py probe
python tools/speaker_purity_pipeline.py evaluate
# 或一次完成
python tools/speaker_purity_pipeline.py all
# 仅登记/导出 worker-selected 候选，不生成手机审核任务
python tools/speaker_purity_pipeline.py queue
python tools/speaker_purity_pipeline.py recrop --help

python -m pytest tests/test_enrollment_purity_gate.py tests/test_v3_migrations.py
$env:ALLDAY_PRIVATE_PURITY='1'
python -m pytest tests/test_enrollment_purity_gate.py
```

私有输出位于 `outputs/enrollment-purity-gate-20260930/`，包括用户要求的迁移、coverage、comparison、features、evaluation、四组消融、regression、migration plan、failure cases、verification、private report 与 root-cause analysis。原音、embedding、source range、review data、真实人物映射均留在 Git ignored 路径。

Migration 前通过 SQLite backup 保存可恢复副本，迁移/执行前后核对人物事实、prototype、授权、政策、旧审核与生产 decision 等表的指纹。测试覆盖 eligible/excluded、latest revision/undo、dedup/conflict、事务回滚、21→22 重复迁移、原音重提与 provenance、insufficient、recrop 新范围、bounded queue、synthetic probe、grouped leakage 与私有历史回归。

下一阶段结论：**clean profile = NEEDS_MORE_CLEAN_DATA；query purity = PURITY_PROBE_NOT_READY**。需要补足核心人物独立 session enrollment，调查 controlled self 错误，并获得独立完整 query-level gold 后，才讨论单独授权的 canary。Legacy 保持生产默认，本轮未实现或开启 canary。
