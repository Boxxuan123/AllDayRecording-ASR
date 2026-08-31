# V2-C.2：声学富集盲测

> 实现日期：2026-08-28
> 目标：不读取候选模型输出，同时让有限人工时间覆盖足够多的真实语音

## 1. 为什么 V2-C.1 不够

V2-C.1 用 `SHA-256(protocol + input fingerprint + seed)` 在 2 小时 44 分录音中均匀选择一个连续 30 分钟范围。它严格独立于所有模型，但只保证“盲”，不保证语音信息量。真实范围 `01:41:42–02:11:42` 的六块平均音量约为 `-48.4/-68.6/-51.6/-65.3/-69.3/-51.9 dBFS`；以 `-50 dBFS`、持续 0.3 秒作为非静音代理时，每块只有约 `1%–2%`。

这段不删除，也不改写。它重新定位为 **ambient-negative track**：用于统计静音、底噪和远处电视条件下的 VAD false alarm、ASR hallucination 和每小时虚假字符数；它不能承担主 ASR CER 排名。

## 2. 两条冻结轨道

| 轨道 | 选择方式 | 主要回答 | 不应用来回答 |
| --- | --- | --- | --- |
| Ambient negative | V2-C.1 均匀连续 30 分钟 | 全天安静时是否误报、幻觉 | 有人讲话时哪个 ASR 文本更准 |
| Speech enriched | V2-C.2 波形声学排序的分散短块 | 高活动真实场景中的 VAD/ASR 质量 | 全日时间加权误报率 |

报告必须分轨呈现，不把富集集的 VAD false-alarm rate 外推为全天发生率，也不把环境负样本的零 transcript 当成 CER 证据。

## 3. V2-C.2 选择器

命令：

```powershell
allday-asr benchmark init-blind-v2c2 1 `
  --name watch-speech-enriched-10m-v2c2-20260828 `
  --review-duration 10:00 `
  --chunk 1:00 `
  --minimum-gap 1:00 `
  --seed v2c2-primary-20260828
```

选择器把不可变原音临时解码为 16 kHz mono PCM，以一分钟为固定候选；每个候选只计算：

- 100 ms 帧超过“全局 20 分位噪声底 + 10 dB”的比例；阈值限制在 `-65～-25 dBFS`；
- 最长连续活动占比；
- 整块 RMS dBFS 和 100 ms 帧 P90 dBFS；
- `200–4000 Hz` 能量占 `80–7500 Hz` 可听频带能量的比例。

五项先在整段录音候选内转成 percentile rank，再按 `0.50/0.20/0.15/0.10/0.05` 加权。按总分降序贪心选择，并要求窗口之间至少留下指定 gap；分数相同时只用固定 seed 产生稳定顺序。选择过程不加载数据库中的 segment、VAD、hypothesis、prediction、speaker 或旧 transcript，也不调用任何语音模型。

完整候选分数保存在 `selection-manifest.json`。任务 metadata 记录其 SHA-256；导入时会重新检查：

- manifest 格式、session、原音 input fingerprint 和 `candidate_asr_outputs_used=false`；
- selected start 与十个 blind window 完全一致；
- 每个窗口的结束时间、声学总分和排名与 manifest 一致；
- manifest、派生 WAV 和原始 source reference 的 SHA-256 均正确。

因此常规网页编辑或单独替换窗口会在导入时被拒绝；若要改变选择范围，必须显式建立带新名称、新 seed 和新 manifest 的修订任务，不能沿用本任务身份。

## 4. 真实任务

任务目录：

```text
state/evaluations/session-000001/
  watch-speech-enriched-10m-v2c2-20260828-blind-v2c2/
```

已固定十个一分钟块：

| 块 | Session 时间 | 全局声学排名 | 活动占比 |
| ---: | --- | ---: | ---: |
| 01 | 00:11:00–00:12:00 | 7 | 89.0% |
| 02 | 00:13:00–00:14:00 | 9 | 91.5% |
| 03 | 00:15:00–00:16:00 | 10 | 92.2% |
| 04 | 00:17:00–00:18:00 | 6 | 90.2% |
| 05 | 00:20:00–00:21:00 | 2 | 95.8% |
| 06 | 00:22:00–00:23:00 | 1 | 93.0% |
| 07 | 00:27:00–00:28:00 | 11 | 89.2% |
| 08 | 00:31:00–00:32:00 | 4 | 94.8% |
| 09 | 00:34:00–00:35:00 | 18 | 84.0% |
| 10 | 00:36:00–00:37:00 | 20 | 73.2% |

独立 FFmpeg `silencedetect=-50dB:d=0.3` 复核得到每块 `86.7%–100%` 非静音代理，明显区别于 V2-C.1 的 `1%–2%`。声学活动不等同于语音，最终 speech/transcript 仍只由人工听取决定。

## 5. 非连续 review-region

`truth_sets` 继续保存第一块开始到最后一块结束的包围范围，兼容 schema v6；十个 `review_region_complete_scope` annotation 才是真正的穷尽复核范围。导入器要求它们与十个 blind window 一一相等且互不重叠。

Benchmark 会先把真值和预测裁到 review-region 的并集：

- VAD 的 `scope_ms` 是十块总时长 600,000 ms，不包含块间空隙；
- 空隙中的预测 speech 不计 false alarm；
- 空隙中的预测 transcript 不计 orphan insertion；
- 每个被复核块内没有人工 speech 的时间仍是真正的负样本，并正常处罚误报。

speech 时间区间可以按 review-region 精确裁剪；transcript 文字不能在没有 token 时间戳时可靠裁字。因此 evaluator 会拒绝任何跨越非连续 review-region 边界的 transcript prediction，要求按每个 review-region 重跑端到端 ASR，或使用 token 时间生成已裁剪的预测快照，避免把块间未听内容算成插入错误。

## 6. 人工操作

```powershell
allday-asr benchmark annotate-blind `
  state\evaluations\session-000001\watch-speech-enriched-10m-v2c2-20260828-blind-v2c2\truth-draft.jsonl
```

页面显示十个一分钟块和总复核时长，不显示声学排名、任何候选 ASR 或旧转写。逐块穷尽标注可听语音，包括电视或其他媒体播放的对白；听不清但确认有人说话时使用 `unintelligible`。每次编辑都会将该块恢复为 pending。十块全部 complete、填写 annotator 并确认 `model_outputs_unseen` 后才能锁定和导入。

每条语音还记录 `speech_source`：

- `live_person`：现场的人正在说话；
- `media_playback`：电视、视频或其他扬声器播放的语音；
- `mixed_live_media`：现场人声与媒体声重叠，人工无需强行拆成两个声道；
- `unknown`：确认是语音，但无法可靠判断来源。

现场与媒体重叠且无法可靠听写时，用 `mixed_live_media + unintelligible` 保留 VAD 事实并排除主 CER。声源选择器加入前已保存的标注按 `live_person` 向后兼容，页面以“现场人声 · 旧标注”提示；新建或编辑后会显式写入 metadata。后续报告必须分别给出全部可听语音、现场语音以及媒体过滤/泄漏结果，不能把正确识别的电视对白算作 ASR 幻觉。

原始 Watch M4A 始终只读；扫描 WAV、十个试听 WAV 和 manifest 都是可审计派生物。

## 7. 模型决策规则

完成标注后：

1. 在人工 transcript 边界上运行 SenseVoice/Qwen/Fun-ASR Oracle ASR，报告原始 CER、ITN CER 和 paired bootstrap，并分列全部可听语音与 `live_person` 子集。
2. 在十个 review-region 上运行各自完整流水线，报告 VAD miss/false alarm、孤立 transcript insertion 和端到端 CER。
3. 环境负样本单独报告 false alarm/hallucination，不与富集集简单平均。
4. V2-C.2 只用于最终比较，不根据结果调整同一批候选后再次宣称是未见测试集；若要调参，必须新建开发集和新的冻结 holdout。

## 8. 已完成五块的预备结果（V2-C.2a）

人工在第 5 块后停止，后五块仍为 pending。系统没有伪造完整盲测声明，而是用 `freeze-completed` 将模型评测前已经标为 complete 的五块派生为独立预备真值：

```powershell
allday-asr benchmark freeze-completed `
  state\evaluations\session-000001\watch-speech-enriched-10m-v2c2-20260828-blind-v2c2\truth-draft.jsonl `
  --name watch-speech-enriched-5m-v2c2a-preliminary-20260828
```

- Truth set：`2`；5 个一分钟 review-region；SHA-256：`922addf409244c194fff1c5133c705ad114a9ac635af8a5d386a0a20a467cea6`。
- 人工真值：19 段、131.993 秒；其中 18 段现场人声、78.858 秒，另有 1 段 53.135 秒的纯净电视节目声音。
- 这只是 **human-limited preliminary subset**，不是完整十分钟 holdout，结果不能宣称最终泛化性能。

### 8.1 同一人工边界的纯 ASR

| 模型 | 全部 raw / ITN CER | 现场人声 raw / ITN CER | 纯电视 raw CER |
| --- | ---: | ---: | ---: |
| SenseVoiceSmall | 44.48% / 43.61% | 49.02% / 47.52% | 38.61% |
| Qwen3-ASR-1.7B | **27.07%** / **28.33%** | **32.35%** / **34.65%** | 20.25% |
| Fun-ASR-Nano | 38.12% / 38.61% | 54.41% / 55.45% | **17.09%** |

在 18 段现场人声上，Qwen 相对 SenseVoice 的 raw CER 差值为 `-16.67` 个百分点，20 万次 paired bootstrap 的 95% 区间为 `[-29.61, -8.12]`；ITN 差值为 `-12.87` 个百分点，区间 `[-27.59, -1.30]`。Qwen 相对 Fun-ASR 的现场 raw CER 低 `22.06` 个百分点，区间 `[-39.38, -9.69]`。这些结果支持 Qwen 作为主识别模型。

Fun-ASR 在唯一一条纯净电视节目上最好，但 `n=1` 的 bootstrap 区间必然退化，不能据此选择默认模型；这条样本只说明媒体声应该独立报告。

### 8.2 当前完整流水线

完整 run 7 已先用 FSMN-VAD 从每个五分钟逻辑窗口提出最长 30 秒的候选，再加 750 ms 上下文交给 Qwen 和 ForcedAligner；此前将它描述为“五分钟整窗直接识别”并不准确。它的实际问题是只有单路 FSMN 判断，而且 padding 内的 token 也会被提交。为避免 token 跨越非连续人工窗口，使用 review-region 裁剪快照：

```powershell
allday-asr legacy asr snapshot 7 --truth-set 2 `
  --name qwen3-asr-1.7b-v2c-full-truth2-scoped
allday-asr benchmark run 2 10
```

| 端到端转写 | raw CER | 替换 / 删除 / 插入字符 |
| --- | ---: | ---: |
| V1 VAD + SenseVoice | 117.68% | 84 / 111 / 231 |
| 五分钟窗 Qwen + 对齐 | **109.94%** | 31 / 107 / 260 |

Qwen 总体低 `7.73` 个百分点，但只有五个配对窗口；按窗口穷举 `5^5` 次有放回重采样，95% 区间为 `[-29.97, +53.66]` 个百分点，优势不确定。更重要的是，排除纯电视窗口后，四个现场窗口中 Qwen 为 `154.90%`、V1 为 `148.53%`，Qwen 反而高 `6.37` 个百分点；电视窗口则是 Qwen `51.90%`、V1 `77.85%`。总体的小幅领先被容易识别的长电视样本明显影响。

诊断很明确：Qwen 将替换错误从 84 降到 31，证明正文识别更强；但旧候选/提交策略把插入错误从 231 增到 260，静音、底噪和推理 padding 内的幻觉抵消了收益。旧 prediction set 10 没有显式 `speech` prediction，因此其 VAD 必须保持 N/A；V2-C.3 的 v4 snapshot adapter 可从 run 7 已冻结的 `speech_ranges_ms` 另建 prediction set 12，重建得到 VAD-F1 `59.74%`、miss `22.32%`、false alarm `64.71%`，不覆盖旧快照。

因此下一步不是退回 SenseVoice，而是建立 V2-C.3：在不修改原音的前提下，以 FSMN 产生高召回候选，用 Silero、相对 SNR 和时长补充证据；边界加上下文供模型推理，并在强制对齐后只提交未加 padding 的已接受核心区。调参使用这五块作为开发证据，另建新的未见 holdout 做最终确认。

### 8.3 V2-C.3 开发结果

最终代码的 run 9 和 prediction set 13 在相同五个 review-region 上得到：

| 场景 | run 7 CER / VAD-F1 | V2-C.3 CER / VAD-F1 |
| --- | ---: | ---: |
| 全部五块 | 109.94% / 59.74% | **92.27% / 61.42%** |
| 四块现场人声 | 154.90% / 56.66% | **116.18% / 60.85%** |
| 一块纯电视 | **51.90% / 68.70%** | 61.39% / 63.00% |

总体改善现在来自现场人声，而不是依赖唯一的纯电视样本。代价是 VAD miss 从 `22.32%` 升到 `27.66%`，且纯电视 CER 回升；不能只报总体 CER 隐藏这些权衡。由于门控阈值已经参考这五块选择，本节是开发集结果，不是新的盲测结论。实现与可复现参数见 [V2-C.3 双 VAD 证据门控](v2-c3-speech-gating.md)。
