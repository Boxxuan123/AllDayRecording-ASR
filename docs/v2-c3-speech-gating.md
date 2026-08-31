# V2-C.3：双 VAD 证据门控与核心 token 提交

> 实现日期：2026-08-28  
> 状态：代码完成；V2-C.2a 五块仅作为开发集，仍需新的未见 holdout 验证

## 1. 修正后的问题定义

run 7 并不是把整份五分钟 PCM 原样交给 Qwen。旧路径已经先用 FSMN-VAD 提出语音段，合并 600 ms 内的间隔，将单段限制在 30 秒，并在两侧加 750 ms 上下文后识别。

真实缺陷有两个：

1. 单个 FSMN-VAD 在电视、底噪和混响下仍产生较多误报；重建到 V2-C.2a 真值后，旧门控的 VAD miss 为 `22.32%`、false alarm 为 `64.71%`。
2. 750 ms padding 是帮助模型识别句首句尾的推理上下文，却被当作最终文字无条件提交。短误报和 padding 内的幻觉因此进入 transcript。

V2-C.3 不更换已经在同边界 Oracle 评测中胜出的 Qwen3-ASR-1.7B，而是把“候选、推理上下文、最终提交”拆开。

## 2. 门控结构

每个五分钟逻辑窗口按以下顺序处理：

1. FSMN-VAD 作为高召回 proposal generator；仍按 600 ms 合并，最长 30 秒。
2. Silero VAD 6.2.1 独立扫描同一临时 PCM，只作为确认信号，不单独创建候选。
3. 为每个 FSMN 候选计算相对窗口 P20 噪声底的 RMS SNR。
4. 候选满足任一条件即接受：时长至少 800 ms、相对 SNR 至少 9 dB、或与 Silero 重叠至少 500 ms。
5. Qwen 推理仍保留两侧各 750 ms 上下文；强制对齐后，只有 token 中点落在“已接受、未加 padding 的 FSMN core”内才提交。
6. VAD prediction 使用已接受 core 两侧各 500 ms 的边界容差；相交范围合并，避免重复计时。

默认阈值集中在 `allday-asr.toml`，并写入每个 processing run 的配置哈希和模型清单。Silero 包版本也随 run 固化。参考实现与接口见 [Silero VAD 官方仓库](https://github.com/snakers4/silero-vad) 和 [FunASR 官方仓库](https://github.com/modelscope/FunASR)。

这是一条偏召回的 OR 门，而不是严格的多模型投票。全天录音中漏掉一句真实对话通常比多保留一个可审计候选更难恢复；最终抑制插入的关键是强制对齐后的 core commit。

## 3. 不可变证据

V2-C.3 不修改原始 M4A，也不覆盖任何旧 run。每个主假设额外保存：

- FSMN core、Qwen inference range 和 Silero range；
- 候选 RMS、噪声底、相对 SNR、Silero 重叠量；
- 接受/拒绝状态与所有命中原因；
- 完整原始文本、接受候选的上下文文本、最终 committed token 文本；
- 每个 token 的候选编号、门控状态、core 范围和是否最终提交。

被拒绝候选的模型输出仍保存在 hypothesis 原始响应和不可变 token 表中，便于复查；`kept_in_core=false`，不会进入最终 prediction。`hypothesis.text`、分歧队列和冻结 transcript 均使用 committed token 文本。

snapshot adapter 升级为 v4。它除了逐 token transcript/alignment prediction，还从不可变 `speech_ranges_ms` 生成显式 speech prediction，因此旧 run 7 也能在不重跑模型的前提下重新计算 VAD 指标。旧 prediction set 不删除、不覆盖。

## 4. V2-C.2a 开发集结果

最终代码真实运行是 run 9：5 个五分钟逻辑窗口、5 份 Qwen 主假设、5 份 Fun-ASR 第二假设、161 个 FSMN 候选，其中 147 个通过门控、14 个拒绝；最终提交 1,301 个主模型 token。prediction set 13 只裁剪到 truth set 2 的五个已完整复核 review-region。

同一代码随后完成全长 run 10：33+33 份主/第二假设、312 个候选、287 个接受、25 个拒绝、2,192 个 committed 主 token，6,147 个审计 token，耗时 460.534 秒且 8 GB 显存未 OOM。全长 prediction set 14 在相同 truth set 2 review-region 上复现下表结果。

| 场景 | V1 CER / VAD-F1 | run 7 CER / VAD-F1 | V2-C.3 CER / VAD-F1 |
| --- | ---: | ---: | ---: |
| 全部五块 | 117.68% / 55.45% | 109.94% / 59.74% | **92.27% / 61.42%** |
| 四块现场人声 | 148.53% / 57.96% | 154.90% / 56.66% | **116.18% / 60.85%** |
| 一块纯电视 | 77.85% / 49.18% | **51.90% / 68.70%** | 61.39% / 63.00% |

相对 run 7：

- 全部 CER 下降 `17.67` 个百分点；
- 现场人声 CER 下降 `38.72` 个百分点，不再依赖电视样本制造总体优势；
- VAD false alarm 从 `64.71%` 降到 `49.66%`；
- VAD miss 从 `22.32%` 升到 `27.66%`，说明门控并非没有召回代价；
- 纯电视 CER 上升 `9.49` 个百分点，但仍优于 V1。

因此，V2-C.3 在这份开发集上解决了最主要的真人场景插入问题，但没有证明阈值已经泛化。五块数据既用于诊断又用于选择阈值，必须明确称为 development evidence，不能再作为未见测试集报告置信结论。

## 5. 运行与复现

```powershell
# 8 GB 5070，模型和 BF16 精度不变
allday-asr legacy asr run 1 --profile compatible-8gb

# 只重跑覆盖当前五块开发真值的前五个逻辑窗口
allday-asr legacy asr run 1 --profile compatible-8gb --max-windows 5

# 生成同时含 speech 与 committed token 的冻结快照
allday-asr legacy asr snapshot <run-id> --truth-set 2 --name qwen-v2c3-dev
allday-asr benchmark run 2 <prediction-set-id>
```

下一次质量结论至少需要一组未参与阈值选择的新录音或新 review-region，并同时报告现场人声、媒体声和安静环境三条轨道。在此之前，V2-C.3 可以作为新候选管线继续开发，但不替换和删除 V1/run 7 的审计结果。
