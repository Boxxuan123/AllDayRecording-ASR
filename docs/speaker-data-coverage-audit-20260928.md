# Speaker Data Coverage Audit：去标识公开报告

## 一句话结论

现有人工人物标注的数量远大于可区分的录音事件：审计到 4,360 条有效人工人物 fact，但按保守连续对话口径只形成 37 个人物事件，跨 14 个已标注 session、11 个日期；项目能够继续积累新事件，但当前流程没有自动保留 blind 数据。

完整逐人表、身份映射、原音定位和专项分析只保存在本地私有 `outputs/speaker-data-coverage-audit-20260928/private-report.md`，本报告不公开它们。

## 直接事实、统计与边界

- **直接数据库事实：** 正式库有 23 个 recording session；有效人工人物 fact 为 4,360 条，均有来源 utterance、session 和原音范围。系统生成身份事实被排除。数据库当前 annotation revision 为 19746，与上一轮冻结验证相同。
- **计算统计：** 15 分钟合并口径得到 37 个人物事件；2 分钟口径得到 97 个。人物事件可以发生在同一录音中，不能相加解释为 37 段相互独立的原音。上一轮自动轨 24 个可评分 prototype 去重只有 11 个历史事件，与本轮“人物标注覆盖事件”不是同一抽样总体。
- **合理推断：** 同 session 的密集标注主要增加已知语音的覆盖密度；新 session、日期和真正新原音对验证更有信息价值。前一轮 20/40/60/90 秒 enrollment 未出现单调增益，故不能把标注量增长直接当作识别增益。
- **当前不可观测：** 数据库没有可靠的设备、环境、说话距离、SNR、重叠语音字段；跨日期不是这些维度的代理。原音虽然可用于将来计算 RMS、削波等客观量，但本轮没有解码全量音频，也不能从这些量单独推断环境或语音纯净度。

事件定义、去重、时区、6 秒可用性门槛和敏感性见[可公开协议](speaker-data-coverage-protocol-20260928.md)。本轮所有精确逐人数据、六张覆盖图和时间曲线均由只读脚本生成，保存在忽略目录。

## 覆盖与边际收益

样本密度和独立性差异很大。最高标注量人物的人物 fact 大部分来自少量 session；在 30 秒、2 分钟、5 分钟和 15 分钟阈值下，事件数会明显变化，故任何单一事件计数都必须附带规则。对最高标注量人物，按录制时序的后 500 条主要增加同一日期的语音密度；按 fact 创建时序的后 500 条则还包含补标的旧录音事件。这两条曲线不能混用。

覆盖状态采用分维度客观规则：去重语音时长 `<60/60–299/≥300` 秒对应 LOW/MEDIUM/HIGH；严格事件数 `<5/5–19/≥20`；session 数 `<3/3–7/≥8`；日期数 `<3/3–7/≥8`。所有人物的 blind coverage 为 NONE。hard negative 支持在本轮事实账本中没有独立量化，前一轮只有一个高度相关的高风险误认事件，不能把重复 prototype 当多次支持。

## 新标注进入 learning 的实际链路

人工 `speaker.assign` 会写入带原音范围的 person fact。移动同步的已应用 `speaker.assign` 会将所属 session 排入 sample queue；worker 从有效 fact 选窗口、生成 embedding、写入当前 sample set 与候选 prototype。**候选不等于可用于人物匹配：** 人工确认后生成 accepted prototype，读取人物向量时还要求 accepted、human_confirmed、有效来源、人物链接和质量条件。直接设备 `assign` 路径本身没有明确排队调用，`retry_samples` 或重新 analyze 可排队；因此并非每次直接标注都会立即进入 sample set。上述路径均没有 learning/blind 分流。[代码](../src/allday_asr/v3/application/annotation_samples.py)及本地 `learning-blind-flow-audit.json` 记录了依据。

冻结时点以后，新 session 为 0、可评分 blind event 为 0。若继续正常标注和样本生成而不先冻结完整新 session，同一来源可以进入 learning，之后不能再宣称独立 blind。离线设计建议：在新 session 录制完成、任何样本生成前锁定整个 session 的 `learning`、`blind` 或 `holdout/undecided` 归属；先为证据稀少人物保留首个有足够语音的新 session，再按实际新 session 到达情况滚动保留，不能按 utterance 随机 20% 切分。此轮未实施分流。

## 六个问题的审计判断

1. **能自然增长独立事件吗？** 能，但取决于新 session、日期和来源出现；同一 session 连续加标注不会等比例增长。
2. **大量标注等于多少独立信息？** 精确逐人表在本地私有报告。主口径是人物、session、15 分钟连续对话与原音重叠去重的事件数，而非 utterance 数。
3. **瓶颈是什么？** 首要是新 session / 日期覆盖、真正独立的 hard negative、以及 blind 保留。设备与环境覆盖无法从现有 metadata 验证。总语音秒数并非所有人物的主要瓶颈。
4. **继续同场景加标注有明显收益吗？** 当前未见可支持明显识别收益的证据；它主要增加既有事件密度。前轮 enrollment 的非单调结果也不支持无限增加同分布语音。
5. **更缺哪类证据？** 新独立人物事件、本人 hard negative、其他低 session 人物及陌生人的新盲测事件，比最高标注量人物的同 session 密集标注更紧迫。
6. **正常使用一周能形成 blind 吗？** 历史有新事件来源，但冻结后目前没有新 session，且无自动 blind 分流；无法可靠预测一周数量。默认流程不能保证得到合格 blind，必须先预留来源。

**决定：有条件地值得继续自然收集。** 收集单位应是新独立事件与完整 session；盲测来源须在 learning 之前隔离。没有据此修改生产算法、阈值、自动确认、数据库或标注。
