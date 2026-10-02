# Self query boundary audit — 2026-10-02

修复合并文本组与连续声学 turn 的 ownership 语义。没有新增 overlap tolerance，没有跳过外人、重叠或短音频保护。

## 基线与审计范围

- ASR / origin master：`ead2a6f38328b5a18f51f22e11be84de66d16c55`。
- Phone / origin master：`06435e3c1dd9963480a15f4c7b1860b142644a9c`；Phone NO CHANGE。
- 固定上轮两个失败案例与原 53-event cohort；没有重新随机挑选。
- 私有 immutable manifest、音频、完整毫秒时间轴、capture SHA 和 revision 在忽略目录 `outputs/self-query-boundary-audit-20261002/`。公开报告只用 A/B 和相对人审起点的毫秒坐标。
- 人审边界只是诊断 oracle。所有自动投影恢复及实际 application 推断在 SQLite 副本运行；production evidence、人工事实与身份未修改。

## Case A：合并文本范围并非一个连续 acoustic turn

Primary root cause：`OWNERSHIP_RULE_TOO_STRICT`。旧规则要求整个文本组落在一个 exclusive turn 内，错误地把文本合并范围当成连续说话范围。范围不一致涉及文本组扩大：`QUERY_BUILDER_RANGE_EXPANSION`；未发现人为 padding 或时间转换 bug。

以人审开始为 0，全部区间使用 `[start,end)`：

| 层 | 相对范围（ms） |
|---|---|
| 人审 clean Self | [0,8000) |
| 原始 utterance | [0,9840) |
| target regular / exclusive 1 | [55,4426) |
| target regular / exclusive 2 | [5405,9235) |
| 原始文本组末尾另一个 target 片段 | [9623,9840)，裁到 utterance 后 217 ms |
| 人审范围内不归 target 的部分 | [0,55)、[4426,5405) |
| 人审范围内 foreign / regular overlap | 无 |

```text
human   0 [------------------------------------------------] 8000
owned    55 [--------------------] 4426   5405 [--------------] 8000
omitted  [55 ms]                    [979 ms]
query       [2185 ms][2186 ms]                 [2595 ms]
```

旧 whole-range containment 拒绝这条 8 秒事件。修复后只取正向 exclusive ownership 的交集，明确省略 55 ms 无归属开头与 979 ms 间隔，不填补间隔。
三个独立物理窗口为 2185 / 2186 / 2595 ms，分数分别为 0.3558189333 / 0.4428307235 / 0.3237201303，全部通过原 Self 阈值。pure full-event 原分数为 0.4393249333。

**精确人审事件诊断：Unknown → Self。原始较长文本组仍 Unknown。** 原始输入最后另有 217 ms 的已归属片段，原窗口最小时长规则仍返回 `incomplete_or_mixed_query / below_minimum_useful_duration`。没有为了接受更长文本组而删掉这段短音频。

## Case B：regular overlap 与人工 clean 范围有冲突

Primary root cause：`OTHER`（regular acoustic composition evidence 与人工 clean 标注不一致）。`DIARIZATION_BOUNDARY_DRIFT` 或 `OVERLAP_FALSE_POSITIVE` 是待声学复核的可能原因，现有证据不足以确认为真实音频中的外人重叠或纯粹模型错误。

| 层 | 相对人审开始范围（ms） |
|---|---|
| 人审 clean Self | [0,8000) |
| 原始 utterance | [0,11655) |
| target regular / exclusive | [-77,11870) |
| foreign regular | [-533,75) |
| foreign exclusive | [-533,-77) |
| regular 两标签同时活动 | [-77,75)，152 ms |
| 与人审范围交集 | [0,75)，75 ms |

exclusive 独占分配已在起点之前转给 target，但 regular evidence 仍预测 simultaneous activity，gate 按 regular 正确拒绝。pure 原分数 0.5398106396 不能证明开头 75 ms 不含外人。
拒绝不是 `end == start`、rounding 或 capture offset 误算；也没有证据证明是错误 revision。保持 Unknown，不忽略 75 ms，不以 exclusive 替代 regular overlap 检查。私有输出提供 75 ms intersection 音频及上下文用于进一步声学复核。

## 坐标、关联和来源核查

两例属于同一已成功 native processing run：run revision 19、input revision 1；current session revision 6、utterance revision 3。人工绑定改变投影，不代表 acoustic artifact 失配。ASR / diarization 为该 run 中唯一 active artifact，校验和有效；其 14 个 input assets 与当前 capture asset 集合一致。transcript artifact 引用相同 ASR / diarization。
artifact 与 capture 没有 numeric revision 列，使用不可变 ID / 状态 / hash；没有虚构 revision 字段。已有 speaker cluster run 的完整关联列于私有 manifest，query 的 turn 来源是 processing run artifact，而非后续 manual cluster。

- A capture session [60000,120000)，source [0,60000)；人审 source [15660,23660)。
- B capture session [540000,600000)，source [0,60000)；人审 source [49265,57265)。原始文本跨到下一 capture 920 ms，foreign 拒绝发生在首 capture 内，与跨 capture 无关。
- 16 kHz 对应 16 samples/ms；capture 起始 sample 与 session offset 相符，没有重复加或漏加 offset。
- pyannote annotation seconds 使用 nearest-ms `round(seconds*1000)`；native turn 不加 ±padding。Qwen forced alignment token 时间加其 analysis-window offset 一次，来自独立边界估计；未发现固定 ASR/diarization 偏移。
- overlap 原来已经是半开区间的严格正交集，相邻端点不误算；本轮未改变 interval semantics。
- 历史文本投影可以合并同标签的多个 turn，这种文本范围不能代替声学 ownership。没有重新生成 ASR、diarization 或历史 transcript。

## 实现与保护

`product_query_ownership.owned_ranges` 合并同 speaker 的精确相邻或相交 exclusive 范围；1 ms 缺口也保留。任意 foreign exclusive 交集直接拒绝整个候选。
product query 在候选全范围检查 foreign regular，之后要求完整有效的 capture coverage，最后才按正向 owned ranges 构造窗口。裁剪无归属部分不能掩盖缺失 capture。现有 source window builder 的 token-tail retry 只作用在 owned span 内。

窗口 provenance 保留 original utterance、selected owned span、omitted ranges 与排除原因；重复物理音频不能变成独立票。既有至少两个有效窗口、每窗质量/时长和全部窗口判定规则均保持。新增 product-only 的 inactive / ambiguous / wrong-session artifact guards；共享 Blind `_evidence` 和实验 window builder 未修改。

## 同 cohort 回放结果

| 对比范围 | 修复前 | 修复后 |
|---|---:|---:|
| pure clean Self full-events（冻结） | 4/4 | 4/4 |
| 4 条精确人审范围的 product-query 诊断 | 2/4 | 3/4 |
| 4 条原始完整 utterance 自动投影诊断 | 2/4 | 2/4 |
| 49 条 clean Non-self 精确范围 False Self | 0/49 | 0/49 |
| 其中 ≥4 秒 Non-self | 0/35 | 0/35 |
| 其中 ≥6 秒 Non-self | 0/27 | 0/27 |
| 固定 24 条当前冻结 matcher 的 paired 诊断，Self | 5/12 | 5/12 |
| 固定 24 条 original production ownership，Self | 2/12 | 2/12 |
| 固定 24 条 Non-self，两种回放 | 0/12 | 0/12 |

24-event before 通过基线源码在当前同数据、同 matcher 下实际重跑。更早存档的 paired 6/12 不用于本轮 before/after 分母比较。以上均为诊断 cohort，受既往 profile-learning exposure 影响，不是新的正式 Blind 或独立 production 准确率。

上轮已知的同一 Non-self 事件 2 秒 / 3 秒相关裁剪，pure 分数 0.1395728327 / 0.1615884587，虽然高于 Self 阈值，真实 production query + matcher + gate 均仍 Unknown：仅一个有效窗口，`insufficient_clean_windows`。不能将两份裁剪计为两个独立负例。

## Historical dry-run 与性能

指定 `limit=100`，当前只存在 24 个可用 session，eligible items 为 2009。同 cohort 的基线源码与修复后源码分别实际只读运行：

| 决策 | before | after |
|---|---:|---:|
| AUTO_SELF | 0 | 0 |
| REVIEW_SELF_CANDIDATE | 0 | 1 |
| KEEP_UNKNOWN | 2009 | 2008 |

此前 2013 items / 4 REVIEW 的旧计划已存在 review 记录，因此当前 cohort 排除了那 4 条；不将旧计划与当前 before 混比。新 REVIEW 只有一个有效 Self 窗口，不自动写身份。
耗时 before 67.592 s / after 84.366 s；embedding query inputs 从 507 增至 681，增加 34.3%，总耗时增加 24.8%。修复后有更多正向 owned 区间可评估，未引入全历史额外查询或逐毫秒循环；这组计时含模型和并行测试开销，不是隔离 microbenchmark。
固定 session 的 query 构建 before 0.0516 s、最终原始-range runtime 0.0479 s。完整真实应用推断副本最终 5.540 s；人审-boundary 副本运行 6.558 s。

**本轮不推荐执行 historical apply。** 没有 AUTO_SELF；新增 REVIEW 应先审核，B 的声学冲突也尚未证实。未运行 apply，也没有启动训练或 ASR/diarization 重建。

## 冻结资产与运行验收

Self matcher: NO CHANGE；Threshold: NO CHANGE；Enrollment: NO CHANGE；Calibration: NO CHANGE；Named-person profiles: NO CHANGE。
Self threshold 0.13760416209697726 / NotSelf threshold -0.006270442157983781；CAM++ single-waveform-v2、FunASR 1.4.4 均未改变。voiceprint、模型资产、profile / enrol / calib / Blind hash 与 before 一致；全部 production SQLite 表逐表 hash 均未改变。

接收服务与 WebUI 在无 active processing job 时按原参数重启。Web 首页通过标准本机 session recovery 后 200，data-health 200、unhealthy replicas 0；receiver TLS 使用原 CA 验证通过，未签名 status 返回预期 401。data-health 的历史 stale artifacts 6998 为状态统计，不是本轮两个当前 active artifact stale。
新进程启动后，分别通过真实 repository / query / provider / matcher / application / correction projection guard 在新数据库副本验收原始 utterance 和精确诊断边界；原始 A/B 都保持 Unknown，精确 A Self / B Unknown，人工 correction 与 annotation facts 一致。线上手工身份不变。

## 验证

- 新增 regression：15 passed。
- self/query/turn、historical backfill、pure matcher 相关测试：109 passed。
- baseline：565 passed、1 skipped、2 failed；final：580 passed、1 skipped、同样 2 failed。
- 两项已有失败均为 `V3ArchitectureTests`：maintenance threshold；domain/application/ports 禁止依赖 adapters。与 baseline 相同，未修改无关架构。
- 完整沙盒运行遇 Windows temp directory WinError 5；原生权限完整重跑得到上述最终结果。
- Ruff（src、tests、本轮 audit tool）、compileall（src 与 tracked test sources）、本轮 py_compile、git diff --check：通过。
- 重复物理音频保护继续由 `test_repeated_physical_audio_never_counts_as_independent_windows` 验证。

只有 product query 解释、一个 ownership domain helper、对应测试和复现工具改变；Phone、冻结 matcher/阈值、Blind policy 与研究资产均无变化。
