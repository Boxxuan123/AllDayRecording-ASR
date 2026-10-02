# Short regular overlap acoustic audit — 2026-10-02

本轮结论：**Case B = INSUFFICIENT_EVIDENCE**。当前证据确认有 75 ms 的 regular 双激活与 target exclusive ownership 冲突，但不能确认这 75 ms 是第二个人的声音，也不能确认它是 diarization 误报。保持 current gate，下一步收集专门的声学审核。

## 实际版本与冻结范围

ASR 基线 / origin master：`bf4937e8a9ec604a3f5321cc58a84837e16ee280`；Phone：`06435e3c1dd9963480a15f4c7b1860b142644a9c`，Phone NO CHANGE。测试在该冻结 production 版本加本轮 audit tooling/tests 下完成；私有 `tested-audit-source-sha256.json` 绑定实际测试源码。

本轮 `src/` 无改动。Self matcher、CAM++、NPZ/reference/enrollment、calibration、threshold、双窗口、minimum duration、ownership、foreign regular/overlap gate、named-person profiles 与 Blind/holdout/independent policy 均 NO CHANGE。未新增 tolerance、未重建 ASR/diarization、未执行 historical apply。

## 固定 Case B 的直接声学材料

复用前轮 immutable manifest 中同一个 session、同一 artifact、同一 capture hash、人审边界和 75 ms 交集，不重新挑案例。前轮 75 ms 文件、本轮 anchor 文件及审核队列 overlap-only 文件的 SHA256 相同。
全部时间以该人审开始为 0，使用半开区间：

| 来源 | 范围 / ms |
|---|---|
| 人审 clean Self | [0,8000) |
| target regular / exclusive | [-77,11870) |
| foreign regular | [-533,75) |
| foreign exclusive | [-533,-77) |
| 两个 regular 标签的 simultaneous activity | [-77,75)，152 ms |
| 与人审范围交集 | [0,75)，75 ms |
| 前一个 automatic utterance | [-2340,-80) |

75 ms 只是完整 152 ms regular 双激活被人审边界裁出的后半段，不是模型固定的“75 ms 尾长”。

私有输出保留 raw 75 ms、±250 ms、±500 ms、±1 s、±3 s context；队列还提供 full-context、target-boundary、overlap-only、pre/post-overlap-context。67 份 clip 均记录实际 session/source 范围、capture ID/SHA、输出 SHA；背景 context 不扩大实际 overlap evidence。每个重点交集有波形和谱图。

Case B raw clip：16 kHz、1200 samples；RMS 0.008584（-41.326 dBFS），peak 0.028595，300–3400 Hz 能量占 0.711943。它有非零录音能量；谱图呈现有结构的能量。**这些指标不能区别人声与所有噪声，也不能识别另一个人的尾音、爆破音或辅音，更不能证明只有 target。**

在 -77 ms exclusive switch 附近，波形仍有连续录音能量；仅凭这一时间点和谱图无法证明一次真实的人物切换。前一段对应自动标签 SPEAKER_01，前一个 utterance 未提供覆盖 foreign-exclusive 片段的严格人物声学审核；没有将 profile/cluster prediction 当作真实人物来源。其真实人物尚未确认。

Case B 分类 `INSUFFICIENT_EVIDENCE`；对时序几何的把握高，对“真实 foreign / 纯 target / 噪声”的声学类别置信度不足。已有 clean_single 是一项人工标注，不是排除 75 ms foreign phoneme 的证明。没有由 pure Self 分数推出“无 overlap”。

## Regular 与 Exclusive 的算法来源

当前 frozen artifact 使用 Community-1 revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`，pyannote.audio 4.0.7。

[生产 adapter](../src/allday_asr/v3/adapters/models/diarization.py) 的 `diarize` 分别读取 `output.speaker_diarization` 和 `output.exclusive_speaker_diarization`（197–200 行附近），没有在 PC adapter 内自行加尾长或 tolerance。

实测安装源码：

1. `.venv/Lib/site-packages/pyannote/audio/pipelines/speaker_diarization.py:684–712`：regular 根据估计的每帧 speaker count 重建；exclusive 用 `count.data = np.minimum(count.data, 1)` 后，以同一 segmentation 与 hard clusters 再重建。
2. 同文件 `reconstruct`（480 行起）按全局 speaker cluster 聚合 local speaker segmentation；两条输出并非独立声学检测。
3. `.venv/Lib/site-packages/pyannote/audio/pipelines/utils/diarization.py:221–268`：活动聚合后按 `argsort(-activations)` 排名，每帧只激活 count 指定的前几个 speaker。
4. powerset 模型经 `Inference` / `Powerset` 转为 multilabel 活动；不能把最终 hard annotation 当作 posterior。

因此 exclusive 可以在同一 regular 双激活帧内只保留活动排名第一的 target。Case B 从 -77 ms 起 exclusive 为 target，符合这种 top-one reconstruction；它不证明另一个 regular speaker 已经声学静音。该帧的实际活动数值与排名差没有保存，不能报告它们。

官方模型卡也将 exclusive 定位为额外输出，用于协调 diarization 与 transcription 时间戳。[Community-1 model card](https://huggingface.co/pyannote/speaker-diarization-community-1#exclusive-speaker-diarization)。本轮算法结论以实际安装的 4.0.7 源码为准，源码 SHA 见私有 `source-code-fingerprints.json`。

## Confidence / posterior 与时间分辨率

- artifact `raw_response` 仅保留 turn count、speaker labels、has_speaker_embeddings；所有 turn confidence 为 null。
- target / foreign activity posterior、overlap-specific confidence：**not available**。无法判断 foreign 是低置信度残留；未伪造或重新拟合。
- 从相同 cached segmentation checkpoint 只加载模型属性，无 audio diarization 重跑：PyanNet、16 kHz、10 s inference window、默认 segmentation step ratio 0.1，即 chunk hop 1 s。
- 模型 `receptive_field.step` = **16.875 ms**，`receptive_field.duration` = **61.9375 ms**。后者是框架的名义 receptive-field 属性；双向 LSTM 还依赖较宽窗口上下文，不能将它当成一个精确边界误差上界。
- 当前配置 `min_duration_off=0.0`，to_annotation 的 min_duration_on=0.0，没有证明存在 ±75 ms padding 或人为固定尾长。
- 75 ms 约 4.44 个 frame hop；完整 152 ms regular overlap 约 9 个 hop。integer-ms annotation 仅是输出精度，不能将 75 ms 解释为 1 ms rounding bug；也不能把几帧当作可以自动忽略的误差。

## Cohort 与计数口径

只读扫描 24 个非 tombstoned session，冻结评估/缺失 learning role 排除 3 个，兼容 acoustic evidence 不可用排除 2 个；实际纳入 **19 个 learning session、7142 个当前原始 active utterance**。不利用自动 identity / cluster / profile 当 ground truth，不纳入冻结评估音频进行本轮声学复核。

“严格人审”要求 current review 为 clean_single、primary person 明确、submit references 一致且无 conflict。按物理 source hash+start+end 去重得到 **83 条**：6 Self、77 Non-self，覆盖 8 位明确 primary person（7 位 Non-self）。它们仍是既有审核源的选择性样本，近邻和子范围相关，不能包装成 83 个独立随机试验。

B-pattern 采用较强定义：候选范围全部由 target exclusive 连续覆盖，foreign exclusive 无交集，同时 regular target 与 foreign 有正交集。所有位置都统计；原请求的边缘子集单独计数。

| 项目 | Count |
|---|---:|
| 全部 fully-owned B-pattern automatic utterance | 802（14 个 session） |
| 其中 START_EDGE / END_EDGE 的事件 | 401 |
| 严格人审 clean_single | 83 |
| 严格人审 B-pattern | 10（2 Self、8 Non-self；7 个 session） |
| 严格 B-pattern 连通 overlap components | 15 |
| 重点音频事件 / components | 9 / 13（2 Self、7 Non-self 事件） |
| 新的专门声学审核 | 0 |

重点队列包含所有严格人审事件中 ≤200 ms 的 overlap，也保留这些事件的其它 connected component；其中少数非 fully-owned B-pattern 作为同源声学对照。不能将 13 个组件当作 13 个独立事件，或把未审核标成 uncertain。

## Disagreement patterns

下表是同一原始 utterance 是否包含对应时间单元的计数；一条可出现在多行，行数不相加成总事件数。

| Pattern | 包含此 pattern 的 utterance |
|---|---:|
| regular target only / exclusive target | 6238 |
| regular target+foreign / exclusive target | 1956 |
| regular target+foreign / exclusive foreign | 744 |
| 严格 B-pattern（全范围 foreign exclusive = 0） | 802 |

自动 B-pattern 的 947 个连通 components 中：START_EDGE 250、END_EDGE 175、INTERIOR 328、FULL 194。严格人审 B-pattern 的 15 个 components 中：START_EDGE 5、END_EDGE 0、INTERIOR 10、FULL 0。全部 83 条严格人审（含 foreign-exclusive 对照）共 32 个 simultaneous components：START_EDGE 8、END_EDGE 1、INTERIOR 23。

START_EDGE / END_EDGE 精确定义为裁后交集触及目标起点 / 终点；INTERIOR 两端严格位于目标内；FULL 为交集占满目标。仅相邻端点不算 intersection；1 ms 正交集仍保留。query edge 不等于真实 speaker switch。

| 连通 overlap duration（ms） | 自动 B components | 严格 B components |
|---|---:|---:|
| (0,25] | 58 | 0 |
| (25,50] | 37 | 0 |
| (50,100] | 88 | 3 |
| (100,200] | 122 | 5 |
| (200,500] | 310 | 4 |
| >500 | 332 | 3 |

自动 B-components：N=947，p25=152 ms、median=330 ms、p75=640 ms、max=4080 ms。严格 B-components：N=15，p25=127 ms、median=196 ms、p75=388.5 ms、max=894 ms。两者均不能作为真实 overlap 时长的分布。

## Speaker transition、方向与固定尺度

按相邻 exclusive 不同标签、精确接触的 switch，且两侧有可对应 regular activity，得到 6313 个 transition。regular 前人结束减 exclusive switch 的 delta：4390 个为 0、1923 个为正。全部 delta 的 p25/median/p75/max 为 0/0/101/43015 ms；正值子集为 152/591/1553/43015 ms。

regular 后人开始早于 exclusive switch 的正 lead 有 1868 个，其正值 p25/median/p75/max 为 118/523.5/1552/42137 ms。二者不是都固定在 50–100 ms；较长值可涉及持续 regular activity 跨过多次 exclusive 分配，不能称为统一短尾。

所有正 tail/lead 与 16.875 ms 整数倍的 residual 都 ≤0.875 ms，符合 ms rounding 后的 frame grid。tail 常见短值为 17 ms（74）、51（58）、34（48）、118（40）、67（38）、135（36）、84（31）、152（31）；75 ms 不是该 transition grid 的固定峰值。

把完整 raw regular 标签对的正交集与保存的邻近 exclusive switch 对齐，自动 947 components 中可关联 435 个：仅 foreign→target 141、仅 target→foreign 86、两方向 208；512 个未在该邻近证据中关联。关联者离 clipped component 最近的 switch 距离 p25/median/p75/max 为 18/51/112/1310 ms。方向由 target 相对 switch 的角色定义，不能用非随机 query 样本证明物理上不对称。

严格 B 的 15 components 中，4 个只关联 foreign→target、1 个两方向、10 个未关联；可关联 5 个的最近距离 p25/median/p75/max 为 77/142/185/1120 ms。因此现象跨多个 session/speaker，且部分对应 speaker change，但**没有证据说它们全部或只集中在边界**。中间的 regular 双激活也必须审核。

## Boundary perturbation：sensitivity analysis only

只改变诊断的起点，不修改 production、人审事实或 acoustic evidence。Case B 起点 delta 与实际 simultaneous 交集如下：

| start delta / ms | simultaneous intersection / ms |
|---|---:|
| -100 | 152 |
| -50 | 125 |
| -25 | 100 |
| 0 | 75 |
| +25 | 50 |
| +50 | 25 |
| +100 | 0 |

-100 时还会纳入 foreign-exclusive 片段，已不满足 full target ownership。+100 时是切掉前 100 ms 的另一个诊断输入，不能据此宣称原 Case B clean，也不是 tolerance。代表样本同样保存起点 ±25/50/100 的完整几何；没有用这些结果调 gate 或阈值。

## 人工声学复核

独立 PC 审核箱 `tools/build_overlap_review_manifest.py` 提供完整上下文、边界、原始 overlap、前/后声音、循环播放，以及“明确第二个人 / 只听到目标人 / 不确定”。结果附音频与 immutable manifest SHA，仅进入 `overlap-review-ledger.json`，不打开生产数据库。

原有 clean_single 83 条不能算成 83 次专门 overlap 复核。以下只统计新审核账本中每个组件的最新明确提交；未复核另列。

| duration / ms | 队列 N | reviewed N | real foreign | target only | uncertain | 未审核 |
|---|---:|---:|---:|---:|---:|---:|
| 0-25 | 1 | 0 | 0 | 0 | 0 | 1 |
| 25-50 | 0 | 0 | 0 | 0 | 0 | 0 |
| 50-100 | 3 | 0 | 0 | 0 | 0 | 3 |
| 100-200 | 6 | 0 | 0 | 0 | 0 | 6 |
| 200-500 | 2 | 0 | 0 | 0 | 0 | 2 |
| 500+ | 1 | 0 | 0 | 0 | 0 | 1 |


本报告收尾时新增专门声学审核为 0，未把 pending 当作 uncertain，也没有代替用户勾选结果；Case B 及重点事件仍分类为 INSUFFICIENT_EVIDENCE。审核箱已生成并打开，可继续积累独立声学记录。

## Case A、historical 与冻结资产验收

通过真实 repository/query/provider/matcher/application 在独立数据库副本重跑：Case A exact human boundary → Self；original utterance → Unknown，217 ms short owned tail 仍排除，production source 未修改。

Historical `dry_run(limit=100)` 结果：AUTO_SELF=0、REVIEW_SELF_CANDIDATE=1、KEEP_UNKNOWN=2008；与上轮当前 baseline 的整个 plan run_id 相同。未 apply。

全部 production SQLite 表、Blind/holdout/independent/learning exposure、profiles/prototypes、reference/enrol/calib/policy/CAM++ 权重的 before/after fingerprint 相同；Community-1 config / segmentation / embedding / PLDA 文件前后 SHA 相同。审核账本和 WAV/图表均在忽略的私有 audit 输出，不进入 production learning。

## Current gate 与建议

支持保持 gate：exclusive 主动压掉第二活动，不构成无 foreign 的声学证据；大量冲突也包含内部与较长 overlap；当前没有专门审核证明大多数短 overlap 是误报。Case B 非零能量与原 clean 标签不能排除短外人音素。

支持未来研究的证据：冲突存在于 Self 和多个明确 Non-self speaker；输出有明确 frame grid，部分 short activity 跨 speaker switch，而 exclusive 为单一 target。这值得专门审核并追踪 activity 输出的来源。

**建议：KEEP CURRENT GATE + COLLECT MORE REVIEW。** 目前不能断言 current gate 明显过严。尚不支持直接 FIX DIARIZATION BOUNDARY 或 DESIGN BOUNDARY UNCERTAINTY MODEL；只有人工声学复核与进一步活动证据形成一致趋势后，才判断这些方案是否值得下一轮研究。即便未来研究 uncertainty，也不等于 overlap<N ms 自动忽略。

## 验证与复现

- 新 audit tests：31 passed；加 Case A/原边界回归：46 passed。
- speaker/query/turn、historical backfill、pure matcher 与 audit 相关集合：140 passed。
- 完整 baseline：580 passed、1 skipped、2 failed；final：611 passed、1 skipped、同样 2 failed。
- 失败列表逐条一致：`V3ArchitectureTests.test_computer_production_sources_stay_below_maintenance_threshold` 与 `test_domain_application_and_ports_do_not_import_ui_http_models_or_sqlite`。未修无关架构。
- Ruff（src/tests、本轮 tools）、compileall（src/tools、tracked tests + 新测试）、py_compile、git diff --check：通过。

```powershell
.venv/Scripts/python.exe tools/audit_short_regular_overlap.py --output outputs/<new-private-audit>
.venv/Scripts/python.exe tools/build_overlap_review_manifest.py --output outputs/<new-private-audit>
.venv/Scripts/python.exe tools/build_overlap_review_manifest.py --output outputs/<new-private-audit> --serve --port 8770
```

默认只读扫描，拒绝覆盖已有 immutable manifest 或写入 production state。审核服务仅监听 127.0.0.1；提交只写独立 audit JSON ledger，没有 identity/profile 更新入口。Phone NO CHANGE。
