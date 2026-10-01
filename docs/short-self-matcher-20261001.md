# Short Speech Self Matcher — 离线诊断（2026-10-01）

## 1. 一句话结论

**INSUFFICIENT EVIDENCE / NO PROMOTION：本轮所有新增短语音候选在一次冻结 holdout 中均出现别人→本人，严格独立短语音数据为零，保持当前生产两窗口规则。**

## 2. Dataset

共 237 个事件：110 Self、127 non-self，12 个 session、9 个日期。事件才是统计单位；重叠 crop 不增加样本量。历史 24 条固定参考包含可能 unsafe 的诊断事件，只用于复现和分析，不能混入候选安全训练集。

| Partition | Self | non-self | Sessions | Dates |
| --- | --- | --- | --- | --- |
| development | 45 | 51 | 6 | 5 |
| holdout | 41 | 41 | 4 | 3 |
| reference | 24 | 35 | 12 | 9 |

| Duration | Self | non-self |
| --- | --- | --- |
| 1-2 | 24 | 24 |
| 2-3 | 27 | 26 |
| 3-4 | 24 | 26 |
| 4-6 | 25 | 27 |
| 6+ | 10 | 24 |

逐事件 provenance 标记：enrollment 0、calibration positive 0、calibration negative 10、profile learning 41、previous diagnostic 24、Blind 0、原始角色 holdout 0。这些标记可能重叠；“原始 holdout”与本轮按日期划分的 holdout 是两个字段。Calibration 关联共映射到 143 个 source range；其事件及同 session 关联风险保守处理。

Development/holdout 按日期与 session 冻结后才计算新 score；两边 session/date 不相交。已知 enrollment、calibration、profile learning、历史诊断和 Blind 事件进入 reference。额外以整个 session 的既往诊断/profile 接触判断严格独立性：development 和 holdout 的严格独立 Self/non-self 均为 **0/0**。82 条 holdout 只是时间隔离的回顾性检验，不能宣称独立验证。Development 的 2–4s Self 仅来自一个 session。

排除计数：foreign/overlap 1783，既有人审 composition 风险 22，不完整 source mapping 83，没有确认安全 source window 1110，自动证据不可用 82，sound 18。保留窗口须受人工 person 身份事实覆盖、自动 exclusive turn 支持、无 foreign regular/exclusive crossing，并完整映射到单一 capture。自动 turn 安全不等同于人工确认每个 crop 的纯净 composition；高分 negative 的结论受当前人工身份 truth 约束。

Split SHA256：`7cfdcfb3c22e0139606ad5906f53e54a63070b8a7bc8ae497b5167808f413ea6`。逐事件边界、truth、provenance、排除原因及 source mapping 仅保存在 ignored 私有 outputs。

## 3. Current Matcher 分布

冻结 CAM++ / FunASR 1.4.4、现有预处理和 matcher；82 个 192 维 reference，20 个原始 enrollment 文件。Self threshold=0.137604162，not-self threshold=-0.006270442。Score 使用现有 0.7 centroid + 0.3 top-reference mean。

下表是 development+holdout、排除 reference 的 full-window 原始分数，固定 **batch=1** 控制 padding 上下文。1–2s 分数仅供诊断，现有 matcher 的最低时长质量门槛仍拒绝它们。

| Bin | Truth | N | Min | P10 | Median | P90 | Max |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1-2 | self | 22 | -0.1014 | 0.0330 | 0.2127 | 0.2843 | 0.3642 |
| 1-2 | non-self | 22 | -0.1197 | -0.0437 | 0.0311 | 0.1481 | 0.2129 |
| 2-3 | self | 21 | 0.0111 | 0.0655 | 0.2491 | 0.3593 | 0.4006 |
| 2-3 | non-self | 17 | -0.0944 | -0.0561 | 0.0182 | 0.1769 | 0.4109 |
| 3-4 | self | 21 | 0.1840 | 0.2115 | 0.2950 | 0.3828 | 0.4047 |
| 3-4 | non-self | 19 | -0.1358 | -0.0909 | 0.0069 | 0.0648 | 0.0997 |
| 4-6 | self | 19 | 0.0987 | 0.1205 | 0.2352 | 0.3872 | 0.4441 |
| 4-6 | non-self | 15 | -0.0807 | -0.0383 | 0.0072 | 0.1330 | 0.3913 |
| 6+ | self | 3 | 0.1909 | — | 0.2099 | — | 0.4494 |
| 6+ | non-self | 19 | -0.1487 | -0.1017 | -0.0414 | 0.0653 | 0.0985 |

≥6s Self 仅 3 条，直接报告原始值：0.209870、0.190909、0.449373，不估计可靠分位数。控制组是所选单 capture 安全窗口，长度最多 8s，并非完整长 utterance。

发现重要批处理混杂：现有 FunASR CAM++ `extract_feature` 会补零到同 batch 最长长度，计算了 lengths；inference 调用 forward 时未传入 lengths，statistics pooling 按整个 padding 时间轴求统计量。同一录音在 batch=8 与 batch=1 下得分可明显不同。因此下面 batch=1 稳定性不能冒充生产 batch=8 的改进。本轮没有修改任何 model/provider/预处理代码，也没有重新校准。历史 batch=8 在原始 24 条顺序下单独精确复现。

## 4. False Self 分析

历史三例的原始生产上下文 score 仍高于阈值，均按人工 person truth 为 non-self；并非重新发现三条 Self。其他列均用同一 batch=1 协议，gap 是加权 self score 减去真实 eligible known-person 库的最大 cosine，**不是经过校准的 posterior margin**。候选库复用实际过滤逻辑，29 个 eligible vector；未使用 35 个未过滤 accepted raw vectors。

| Case | Duration | Full b8 | Full b1 | Best other cosine | Gap b1 | Crop min–max | Crop std | Pair min | Ownership |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 历史 14 | 4.40s | 0.1422 | 0.1181 | 0.6132 | -0.4951 | 0.0748–0.1205 | 0.0195 | 0.5952 | 含 foreign/overlap 风险 |
| 历史 15 | 3.44s | 0.1385 | 0.0280 | 0.6032 | -0.5752 | 0.0280–0.1573 | 0.0466 | 0.5880 | 含 foreign/overlap 风险 |
| 历史 16 | 3.04s | 0.1689 | 0.0057 | 0.5939 | -0.5882 | -0.1018–0.0989 | 0.0687 | 0.2558 | 通过自动 ownership |

历史三例在 batch=1 的 full 与 margin 上容易与 #1/#5 区分，但这不足以推广：扩展 holdout 出现了更高且稳定的 negative。

匿名人物关系：历史 14 与 16 的人工 truth 为同一个 known person A；历史 15 的 truth 为另一个 known person B。三例最近的 eligible known-person 候选均为 A，因此 15 的最近候选并不等于其人工 truth。真实 person IDs 只记录在私有 evidence JSON。

| Held negative | Full | Gap | Crop min | Crop std | Pair min | Ref support |
| --- | --- | --- | --- | --- | --- | --- |
| 2.56s | 0.4109 | 0.0863 | 0.3511 | 0.0216 | 0.8648 | 98.8% |
| 2.72s | 0.1982 | -0.3869 | 0.1985 | 0.0079 | 0.7649 | 57.3% |
| 2.00s | 0.1628 | -0.2743 | 0.1628 | 0.0000 | 1.0000 | 35.4% |

2.56s negative 的 full 0.410948、gap +0.086261、crop min 0.351051、pairwise min 0.864780、reference 支持 98.8%，同时超过 #1 的相应指标。2.00s 事件只有一个 2s crop；std=0、pairwise=1 是同一音频的退化比较，不能算独立证据。Quality 仅 duration/12000；SNR **NOT AVAILABLE**，不作为真实声学纯净度。

## 5. #1 / #5

二者固定为 reference anchor；没有参与拟合、没有以其接受结果反向选阈值。所有下述分数为 batch=1。

**#1：3.04s。** Full=0.387691；crops=0.366350, 0.366840, 0.349248, 0.372770, 0.372870。Crop min/max/mean/median/std/range=0.349248/0.372870/0.365616/0.366840/0.008646/0.023621；full+crops pairwise cosine min/median=0.745769/0.883450。最近真实候选 cosine=0.328623，diagnostic gap=+0.059069；reference 支持比例=91.46%。

Shadow accept：Single normal threshold, High-score, Score+margin, Crop stability, Min-crop, Enrollment consensus。Shadow Unknown：Current 2-window。
**#5：3.76s。** Full=0.322854；crops=0.230988, 0.224658, 0.300329, 0.315176, 0.369053, 0.360185。Crop min/max/mean/median/std/range=0.224658/0.369053/0.300065/0.307753/0.056362/0.144395；full+crops pairwise cosine min/median=0.617665/0.844266。最近真实候选 cosine=0.334024，diagnostic gap=-0.011170；reference 支持比例=90.24%。

Shadow accept：Single normal threshold, High-score, Min-crop, Enrollment consensus。Shadow Unknown：Current 2-window, Score+margin, Crop stability。

更稳定或更高 margin 可以拒绝部分真正 Self（尤其 #5），同时仍接受扩展 high-score negative。不能仅凭这两个 anchor 认定证据结构安全。

## 6. Candidate Comparison

每个证据家族仅一个 development 提案；无 grid search，参数在 holdout scoring 前冻结，holdout scoring/evaluation 各只执行一次。完整条件与版本 `short-self-shadow-v1` 保存在私有 candidate-rules.json。

参数：T_short=0.138609290（dev 最大 short negative + 固定 0.02，下限为 T）；M_short=0.000；crop passes≥2、std≤0.028607087；min-crop≥0.137604162；reference 支持比例≥0.121951220。T_short 仅略高于现有 T，不能称为已证明“高置信”的阈值。Margin 使用真实候选，但不同聚合量的差值需要额外校准。

下表 holdout 仅 **2–4s：22 Self / 13 non-self**。最后一列是 development 20 Self / 23 non-self 的接受/误接收计数。短候选都保留 ownership 与 embedding availability 门槛；≥4s 保持现有双独立窗口条件。

| Rule | Self coverage | Self Unknown | Negative→Self | Holdout result | Dev Self; FA |
| --- | --- | --- | --- | --- | --- |
| Current 2-window | 0/22 (0.0%) | 22 | 0/13 | 保守基线 | 0/20; 0/23 |
| Single normal threshold | 21/22 (95.5%) | 1 | 3/13 | 未满足零误接收 | 18/20; 0/23 |
| High-score | 21/22 (95.5%) | 1 | 3/13 | 未满足零误接收 | 18/20; 0/23 |
| Score+margin | 4/22 (18.2%) | 18 | 1/13 | 未满足零误接收 | 1/20; 0/23 |
| Crop stability | 12/22 (54.5%) | 10 | 2/13 | 未满足零误接收 | 10/20; 0/23 |
| Min-crop | 21/22 (95.5%) | 1 | 2/13 | 未满足零误接收 | 14/20; 0/23 |
| Enrollment consensus | 21/22 (95.5%) | 1 | 3/13 | 未满足零误接收 | 18/20; 0/23 |

按时长分别报告 holdout（2–3s：10 Self/8 negative；3–4s：12/5；4–6s：9/8）：

| Rule | Bin | Self accepted | Self Unknown | Negative→Self | Negative rejected | Coverage |
| --- | --- | --- | --- | --- | --- | --- |
| Current 2-window | 2-3 | 0 | 10 | 0 | 8 | 0.0% |
| Current 2-window | 3-4 | 0 | 12 | 0 | 5 | 0.0% |
| Current 2-window | 4-6 | 6 | 3 | 1 | 7 | 66.7% |
| Single normal threshold | 2-3 | 9 | 1 | 3 | 5 | 90.0% |
| Single normal threshold | 3-4 | 12 | 0 | 0 | 5 | 100.0% |
| Single normal threshold | 4-6 | 8 | 1 | 2 | 6 | 88.9% |
| High-score | 2-3 | 9 | 1 | 3 | 5 | 90.0% |
| High-score | 3-4 | 12 | 0 | 0 | 5 | 100.0% |
| High-score | 4-6 | 6 | 3 | 1 | 7 | 66.7% |
| Score+margin | 2-3 | 3 | 7 | 1 | 7 | 30.0% |
| Score+margin | 3-4 | 1 | 11 | 0 | 5 | 8.3% |
| Score+margin | 4-6 | 6 | 3 | 1 | 7 | 66.7% |
| Crop stability | 2-3 | 6 | 4 | 2 | 6 | 60.0% |
| Crop stability | 3-4 | 6 | 6 | 0 | 5 | 50.0% |
| Crop stability | 4-6 | 6 | 3 | 1 | 7 | 66.7% |
| Min-crop | 2-3 | 9 | 1 | 2 | 6 | 90.0% |
| Min-crop | 3-4 | 12 | 0 | 0 | 5 | 100.0% |
| Min-crop | 4-6 | 6 | 3 | 1 | 7 | 66.7% |
| Enrollment consensus | 2-3 | 9 | 1 | 3 | 5 | 90.0% |
| Enrollment consensus | 3-4 | 12 | 0 | 0 | 5 | 100.0% |
| Enrollment consensus | 4-6 | 6 | 3 | 1 | 7 | 66.7% |

Holdout 全时长 41 Self/41 non-self 的 Negative→Self 依次为 **1、5、4、2、3、3、4**；current two-window 有一例 ≥4s 控制窗口误接收，不能宣称其扩展集合绝对零风险。这是 crop-level 离线模拟，未运行这些扩展事件的正式产品完整 ownership eligibility，也没有修改产品身份。

Holdout short 最大 Self full=0.404731，最大 negative full=0.410948。在本已冻结回顾性集合上，任何能排除该 negative 的纯 full-score 阈值也会排除全部 22 条短 Self。这是分数排序描述，未据此拟合或追加 holdout 候选。

## 7. Fixed 24 Regression

复用原始 24 条（12 Self / 12 non-self），真实 provider 默认 batch=8，两个路径严格分开：

| Path | Self | Self Unknown | Negative→Self |
| --- | --- | --- | --- |
| Paired diagnostic | 6 | 6 | 0 |
| Actual production ownership builder | 2 | 10 | 0 |

实际 ownership 路径仅在内存数据库克隆中恢复 automatic track/去掉人工身份投影，再调用真实 builder；真实人工 truth、identity、correction 和 profile 保持不动。两条路径不能互相替代。三例历史 single-window False Self 也已精确复现；batch=1 的不同结果没有修复生产误接收。

## 8. 是否存在可推广候选

**INSUFFICIENT EVIDENCE**。同时记录 `INSUFFICIENT INDEPENDENT SHORT-SPEECH DATA`、`NO PROMOTION`。本轮全部新增短规则均在 holdout 产生误接收；独立开发/holdout、扩展 negative 零新增 False Self、独立 Self gain 的推广门槛未成立。没有启用生产候选，没有用 Blind V2 拟合。

## 9. Blind / Profile Integrity

**unchanged**：29 个保护表逐表指纹一致，包含身份、correction、learning、profile、Blind、utterance 和 speaker-cluster 状态。上轮 22 个保护表与 policy/NPZ/model hash 同样一致；20 个原始 enrollment 文件与已记录 hash 全部一致。Active policy、阈值、82 references、192 维、CAM++、FunASR 1.4.4 均冻结。

仅新增通用审计工具、匿名合成测试和本文档。没有 src 生产改动、Phone 改动、重启或人工 truth 写入。私有音频、SQLite、NPZ、人物/session/event IDs 和诊断输出均不提交。

相关测试最终 77 passed / 1 skipped；全量 `pytest tests` 最终 444 passed / 1 skipped / 2 failed。两项失败来自原有 architecture 检查（生产文件长度与 application 导入 adapter），所涉及 tracked 代码未改动。Ruff、compileall src、py_compile 审计工具与测试、git diff --check 通过。

## 10. 下一步

选择 **E：继续自然积累独立 session/date 的可信人工身份短样本，保持当前生产规则**。至少需要未用于 enrollment/calibration/profile-learning/旧诊断/Blind 的新 Self 与 non-self session，先冻结划分与版本再做下一轮；本轮 holdout 不再拟合。批处理 padding 上下文是已发现的混杂，后续研究须单独冻结推理协议并验证，不能把本次 batch=1 结果直接上线。当前数据不足以把主因归给新模型、profile aggregation 或 diarization，因此不选择 B/C/D。

## 通用审计工具使用

入口 `tools/audit_short_self_matcher.py`，按以下顺序使用新的 ignored output 目录；所有 phase 都要求 `--state-dir <runtime-state>` 与 `--output <private-output>`：

1. `prepare --previous-manifest <fixed-regression-manifest> --previous-trace <previous-trace> --legacy-db <optional-legacy-db>`：只读准备，冻结 source/provenance、session/date split 和资产指纹。
2. `score --partition development`，然后 `fit`：仅开发集产生少量固定规则。
3. `score --partition holdout`，然后 `evaluate --partition holdout`：一次 holdout；重跑/覆盖会拒绝。
4. `evaluate --partition development`；`score --partition reference`；`evaluate --partition reference`：参考案例独立报告。
5. `regress --previous-manifest <fixed-manifest> --previous-replay <previous-replay>`：batch=8 固定回归，真实 ownership builder 仅操作内存克隆。
6. `report`；`verify`：报告与 29 个表/资产的读前读后比较。评分 JSON、rule snapshots 和 SHA 全留在私有 output。

这些工具仅用于离线 shadow；任何通过都不自动改变生产规则。
