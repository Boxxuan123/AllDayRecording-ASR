> 历史任务说明，留存用于审计。对应结果见 [2026-09-28 实验报告](../speaker-recognition-benchmark-v2-20260928.md)，后续协议见 [Phase 4](../speaker-verification-phase4-20261001.md)。本文件不是当前待执行任务。

你现在位于 `AllDayRecording-ASR` 仓库。请基于仓库**最新代码、当前正式数据库和已有离线实验数据**，执行一次完整的：

# Speaker Recognition Benchmark V2

目标不是修改生产系统，而是通过严格、可复现的离线实验，找出最适合本项目“全天录音 + 日常人工标注逐渐认识熟人”场景的人物声纹识别方案。

# 0. 核心背景

这个项目不是普通 speaker verification。

最终需求是：

```
全天录音
→ diarization / speaker track
→ 跨时间累计说话人证据
→ 识别已经认识的人
→ 对无法可靠判断的人保持 Unknown
→ 人工标注后逐渐改善人物 voice profile
→ 后续用于人物记忆、每日总结、关系分析、待办来源等
```

因此：

## 错误代价不是对称的

本项目中：

```
Unknown → Unknown
```

只是漏识别，可以之后人工标注。

但：

```
真实人物 A → 错误人物 B
```

会污染：

- 人物历史
- 对话归属
- 每日总结
- 关系分析
- 长期人物记忆
- 待办来源

因此实验评价中：

**False Accept / Wrong Person Assignment 的优先级明显高于 False Reject。**

不要单纯以 overall accuracy 或 recall 最高作为“最佳方案”。

# 1. 已知历史实验

此前已经完成过一次离线可行性实验，请首先审查并复用它，不要无意义地从零重造。

已有目录大致为：

```
outputs/annotation-feasibility-20260928/
docs/annotation-feasibility-pilot-20260928.md
docs/annotation-recognition-diagnosis-20260928.md
```

已有实验曾提取：

```
555 个原音窗口
```

并按日期隔离 train / validation / test。

历史结果大致为：

```
现有平均声纹 + 固定阈值：
known 0/22
unknown false accept 0/21

平均声纹 + validation 重新校准：
known 16/22
unknown false accept 0/21

多种 multi-prototype：
known 约 10~12/22
unknown false accept 约 1~2/21
```

此外在真实自动 speaker track 的代表声纹上，曾发现：

```
本人声音 → 被误认为张卫平
```

这是当前最重要的 failure case 之一。

请先核实这些数字、数据划分和历史实验实现，不要直接相信文字总结。

# 2. 本轮原则

## 禁止

本轮不要：

- 修改生产识别流程
- 修改正式数据库
- 修改正式标注数据
- 调低生产阈值
- 自动开启任何人物 auto-confirm
- 为了提高数字而污染 train / validation / test 隔离
- 使用 test 数据选阈值或选择方法
- 因为论文方法听起来更高级，就默认它更好

## 允许

允许：

- 新建独立实验代码
- 新建只读分析工具
- 读取正式数据库
- 读取已有音频
- 重新提取 embedding
- 建立新的冻结数据集
- 生成 JSON / CSV / Markdown / 图表
- 比较不同 backend / profile / calibration 方法

所有生产数据访问尽量使用 readonly。

# 3. 第一步：审计现有 benchmark

先检查：

1. 555 个窗口是否仍然存在并可复现。
2. train / validation / test 是否真正按：
   - session
   - 日期
   - 原始音频范围
     隔离。
3. 是否有同一原音或高度重叠音频泄漏到多个 split。
4. 人物标签是否来自有效人工标注。
5. unknown / distractor 是否确实未进入 enrollment。
6. 历史实验有没有 test-set tuning。
7. embedding 是否 finite、归一化正常。
8. 当前 speaker embedding 模型、版本和参数是什么。

如果发现历史 split 有明显问题，重新冻结一版：

```
benchmark-v2 manifest
```

但必须记录：

```
为什么重建
旧版哪里有问题
新版如何避免
```

# 4. Ground Truth 覆盖审计

当前最大的限制之一，是完整标注的真实自动 speaker track 数量不够。

请重新统计：

```
自动 diarization / speaker track
↓
representative audio range
↓
其中多少可以根据当前人工 person facts 得到明确 ground truth
```

分类至少包括：

```
fully_labelled_single_person
multiple_person_labels
incomplete_labels
unlabelled
```

对于：

```
fully_labelled_single_person
```

建立独立的：

```
track-level held-out benchmark
```

如果当前数量仍然太少：

不要假装已经完成 end-to-end 验证。

但仍继续完成 window/cluster-level benchmark。

最终报告明确区分：

```
controlled window benchmark
real automatic-track benchmark
```

# 5. 固定基础 embedding

第一阶段不要换 encoder。

优先使用生产系统当前的 speaker embedding 模型。

目标是回答：

> 当前主要问题究竟来自 embedding，还是 profile / calibration / temporal decision backend。

只有完成后端实验后，才决定是否值得测试新 encoder。

# 6. 实验组 A：Calibration / Open-set Identification

以当前表现最好的：

```
single representative person embedding / average voice profile
```

作为基础。

至少比较：

## A0 — Current baseline

```
cosine similarity
fixed threshold = 当前生产阈值
当前 margin 逻辑
```

## A1 — Global calibrated threshold

只使用 validation：

选择全局 threshold。

禁止使用 test 调参。

## A2 — Per-person threshold

为每个熟人分别从 validation 估计 acceptance threshold。

例如：

```
妈妈 threshold != 张卫平 threshold
```

当某个人 validation 数据不足时：

必须设计 fallback：

```
global threshold
或 shrinkage toward global threshold
```

不能过拟合两三个样本。

## A3 — Margin / Hard Negative

除了：

```
best_score
```

还使用：

```
best_score - second_best_score
```

并重点加入 hard negatives：

```
本人
其他经常出现的熟人
最相似的人
```

特别检查历史 failure：

```
本人 → 张卫平
```

是否能够被 margin / hard-negative 策略挡住。

测试组合：

```
score only
score + fixed margin
score + calibrated margin
per-person threshold + margin
```

## A4 — Score Normalization

实验：

```
raw cosine
AS-Norm
```

如果现有数据允许，也可以测试简单 cohort normalization。

注意：

cohort 必须只来自 train / enrollment 范围，禁止使用 test identity information。

## A5 — Robust / GMM Calibration

参考近期 Target Speaker Tagging 中的 robust score calibration 思路。

不要机械复制论文。

目标是测试：

> 同一个 speaker cluster 中存在异常短片段或 diarization 污染时，GMM/robust aggregation 是否优于 simple average。

至少比较：

```
mean score
median score
trimmed mean
top-k mean
2-component GMM based robust estimate
```

如果数据不足以可靠拟合 GMM，必须明确说明，不要强行得出结论。

# 7. 实验组 B：Voice Profile / Enrollment

核心问题：

> “标注越来越多”究竟怎样转化成越来越可靠的人物 profile？

不要默认越多越好。

对于数据量足够的人物，至少测试：

```
20 s
40 s
60 s
90 s
全部可用历史标注
```

每种时长再比较：

## B1 — Random enrollment

随机抽取。

多次 seed 重复，报告均值和方差。

## B2 — Longest / clean-ish enrollment

优先：

```
较长
少 overlap
质量较高
```

## B3 — Diverse-by-session/date

在相同时长预算下，优先覆盖：

```
不同日期
不同 session
不同录音条件
```

避免全部来自一段录音。

## B4 — Quality-selected enrollment

构造简单可解释的 quality score。

可考虑：

```
duration
SNR / energy stability
clipping
overlap
VAD purity
embedding consistency
```

不要因为没有某一个指标就停止实验。

有多少可靠特征就先做多少。

# 8. Profile Representation

在完全相同 enrollment 数据上比较：

```
single mean centroid
weighted mean centroid
median / robust centroid
2 centroids
3 centroids
all individual prototypes
```

重点回答：

> 上一轮 multi-prototype 为什么比 single mean 更差？

请尝试定位：

```
是否 prototype 数量导致 false accept 上升
是否包含低质量样本
是否 condition mismatch
是否 scoring 方式有问题
```

不要只报告数字。

# 9. 实验组 C：Quality-aware Scoring

比较：

## C0

```
所有语音平等
```

## C1

```
低于 quality threshold 的片段直接 abstain / Unknown
```

## C2

```
根据 quality 对 score 降权
```

## C3

```
根据 quality 动态提高 acceptance threshold
```

目标不是实现论文级 uncertainty model。

先回答简单 quality-aware 规则有没有真实收益。

# 10. 实验组 D：Temporal / Cluster Aggregation

这是本轮非常重要的一组。

当前项目不需要在每个 0.5~1 秒片段出现时立刻认人。

因此比较：

## D0 — utterance immediate decision

每个短片段独立认人。

## D1 — speaker-cluster aggregation

同一个 diarization local speaker cluster：

```
累积多个片段
→ 聚合 embedding 或 similarity score
→ 再决定 person
```

至少测试：

```
累计 3 s
5 s
10 s
20 s
```

或者根据真实数据调整合理阈值。

## D2 — delayed commitment

不要第一段出现时立即永久赋予人物。

模拟：

```
insufficient evidence
→ pending

evidence accumulated
→ known person

ambiguous
→ Unknown
```

至少记录：

```
time-to-identification
false person assignment
known recall
unknown rejection
```

目标是回答：

> 多等几秒能否显著减少错误人物识别？

如果：

```
5~10 秒后 false accept 大幅下降
```

这对项目非常有价值。

# 11. 实验组 E：Continual Learning Simulation

这是最贴近产品目标的实验。

按照时间顺序模拟：

```
Day 1
Day 2
Day 3
...
```

不要一次把所有未来标注提前放进 profile。

模拟过程：

```
当前已有 profile
→ 识别当天数据
→ 人工确认部分 Unknown / 错误
→ 更新 voice profile
→ 下一天重新识别
```

记录：

```
随着标注天数增加：
known recall 是否提高
false accept 是否降低
人物 threshold 是否稳定
需要人工标注的比例是否降低
```

特别关注：

> performance 是否随着 enrollment 增加单调提高。

如果出现：

```
60s 最好
90s 持平
全部历史数据反而下降
```

必须明确指出。

这将直接决定生产系统未来是否：

```
无限累积历史
```

还是：

```
维护一个有限、高质量、覆盖不同条件的 voice profile
```

# 12. End-to-end Track Probe

将前面 validation 选择出的最佳 1~3 个方案，应用到：

```
真实自动 diarization track
```

而不是只用人工切好的窗口。

只使用已经具有可靠 ground truth 的 track。

必须单独报告：

```
known correct
known rejected
wrong known identity
unknown correctly rejected
unknown falsely accepted
self falsely accepted as other
```

尤其检查：

```
本人 → 张卫平
```

是否仍发生。

如果真实 track benchmark 样本量太少：

明确写：

```
insufficient for production acceptance
```

不要用 controlled window benchmark 代替 end-to-end 结论。

# 13. 评价指标

至少输出：

```
Known identification accuracy
Known recall
False reject rate
Unknown false accept rate
Wrong-person assignment rate
Macro per-person recall
Per-person false accept
```

还要额外给一个符合本项目风险偏好的指标。

例如：

```
Cost =
    wrong_person * 10
  + unknown_false_accept * 10
  + false_reject * 1
```

具体权重可以另外报告敏感性分析：

```
5:1
10:1
20:1
```

不要只用一个人为权重决定最终结论。

# 14. Confidence / Statistical Stability

数据量允许时：

使用：

```
bootstrap CI
multiple random seeds
```

尤其 enrollment sampling 实验必须多 seed。

不要因为：

```
16/22 vs 17/22
```

就武断宣布新方案更好。

明确区分：

```
clear improvement
possible improvement
no meaningful difference
insufficient evidence
```

# 15. 结果选择规则

最终不能简单说：

```
准确率最高方案就是最佳
```

候选方案必须满足优先级：

1. Wrong-person assignment 尽可能接近 0
2. Unknown false accept 尽可能接近 0
3. 在上述约束下提高 known recall
4. 方法足够稳定
5. 不依赖 test tuning
6. 复杂度合理
7. 延迟适合本项目
8. 能支持未来 continual learning

如果一个方法：

```
Recall +5%
但是 false identity 从 0% → 4%
```

本项目通常应视为退步。

# 16. Encoder 对比：只有满足条件才做

不要一开始就测试新模型。

如果 backend 最优方案后仍然发现：

```
大量 failure 来自
极短音频
远距离
噪声
channel mismatch
embedding 本身不可分
```

再做一个小规模 encoder A/B。

候选可以根据当前环境已有模型选择，例如：

```
当前生产模型
ECAPA-TDNN
CAM++
ERes2Net / ERes2NetV2
WavLM-based speaker embedding
```

不要为了“模型越多越好”下载几十 GB 模型。

最多选 2~3 个有明确理由的候选。

而且所有 encoder 必须使用：

```
相同 split
相同 enrollment
相同 backend protocol
```

# 17. 实验输出目录

新建：

```
outputs/speaker-recognition-benchmark-v2-20260928/
```

至少包含：

```
PROTOCOL.md

manifest.json
split-audit.json

baseline-results.json

calibration-results.json
profile-results.json
quality-results.json
temporal-results.json
continual-learning-results.json

track-level-results.json

failure-cases.json

summary.csv
per-person.csv

verification.json
```

如有必要增加：

```
plots/
```

图表建议包括：

```
enrollment seconds vs known recall
enrollment seconds vs false accept
threshold vs FAR/FRR
per-person score distributions
known vs unknown score distributions
margin distributions
performance over simulated days
```

# 18. 最终报告

生成：

```
docs/speaker-recognition-benchmark-v2-20260928.md
```

报告必须回答下面这些问题。

## Q1

当前最大瓶颈到底是：

```
embedding
profile construction
calibration
unknown rejection
short segments
diarization contamination
temporal decision
```

中的哪些？

按证据说明，不要凭感觉。

## Q2

是否存在明显优于：

```
average embedding + calibrated threshold
```

的方案？

## Q3

AS-Norm 是否值得进入生产？

## Q4

per-person calibration 是否显著优于 global threshold？

## Q5

hard-negative / margin 是否解决：

```
本人 → 张卫平
```

这种错误？

## Q6

GMM / robust aggregation 是否有实际收益？

## Q7

speaker cluster aggregation 是否优于单 utterance identification？

## Q8

delayed commitment 是否值得采用？

最好给出：

```
等待多少秒
换来多少 false accept 降低
代价是多少 recall / latency
```

## Q9

一个人物最合理的 enrollment 规模是多少？

例如：

```
20s
40s
60s
90s
```

是否存在明显收益饱和点？

## Q10

更多标注是否真的持续提升人物识别？

还是：

```
精选有限 voice profile
```

优于：

```
无限累积所有历史样本
```

## Q11

multi-prototype 上次为什么失败？

有没有条件下它重新优于 single centroid？

## Q12

现在是否已经有足够证据修改生产人物识别模块？

结论只能属于：

```
A. 已有明确证据，可以进入生产设计
B. 有改善信号，但真实 track 验证仍不足
C. 当前方案没有稳定改善，不建议修改生产系统
```

必须说明理由。

# 19. Failure Case 分析

不要只给 aggregate metrics。

至少挑出：

```
所有 wrong-person assignment
所有 unknown false accept
最典型 false reject
本人相关错误
高分但认错的样本
低质量污染样本
```

记录：

```
truth
prediction
score
second best
margin
duration
quality
session/date
profile configuration
```

如果可以定位原音范围，也保留可追溯信息。

# 20. 最后给我一个简洁的人类可读结果

任务完成后，终端最终回复必须包含：

## 1. 一句话结论

例如：

```
最佳方案仍是 single centroid，但加入 per-person calibration + hard-negative margin + cluster aggregation 后，在保持 unknown FAR=0 的情况下，known recall 从 X 提升至 Y。
```

这里只是格式示例，不要预设结果。

## 2. 最重要的结果表

至少列出前 5 个真正值得比较的方案：

```
method
known correct
known recall
wrong person
unknown false accept
false reject
```

## 3. 最佳方案组成

逐项写清：

```
embedding =
profile =
enrollment seconds =
scoring =
normalization =
threshold =
margin =
quality =
cluster aggregation =
delayed commitment =
```

## 4. 是否建议改生产代码

只允许：

```
建议
暂缓
不建议
```

并说明最主要证据。

## 5. 下一步

最多给 3 个最值得做的动作。

# 21. 可复现性

必须保证别人以后能运行：

```
python ... benchmark ...
```

重新得到核心结果。

保存：

```
Python version
dependency versions
model name/version
model hash if possible
dataset manifest hash
random seed
git commit
```

# 22. 不要为了完成任务而“报喜”

如果最终发现：

```
所有复杂方案都不如简单平均声纹
```

这是非常有价值的实验结果。

如果发现：

```
现有数据不足以判断 GMM / delayed commitment
```

也直接说。

如果出现：

```
controlled benchmark 很好
real track benchmark 很差
```

以真实 track 风险为优先，不要用前者掩盖。

我们的目标不是证明某个新方法好，而是避免把错误方案重构进生产系统。

# 23. Git / 工作区

本轮原则上只新增：

```
实验代码
实验数据结果
报告
```

不要修改生产业务代码。

完成后运行：

```
git status
git diff --stat
```

明确列出所有修改文件。

**本轮暂时不要为了实验而修改、提交或推送生产逻辑。**

如果你认为某个生产缺陷导致 benchmark 本身无法继续：

停止对该生产代码的修改，只在最终报告中记录：

```
blocking production defect
证据
建议修复
```

实验能绕过的则在实验层绕过。

现在开始执行，不要只写方案。

先审计已有实验与数据，然后实际运行 benchmark。

过程中如果某一高级方法数据不足，可以标记为 insufficient evidence，但不要因此停止整个 benchmark。

最终把完整实验结果、报告路径、关键数字和生产修改建议返回。
