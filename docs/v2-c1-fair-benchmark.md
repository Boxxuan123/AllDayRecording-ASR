# V2-C.1：公平基准重建

> 实现日期：2026-08-28
> 结论边界：旧 15 分钟真值只用于诊断；模型晋级必须等待独立盲标连续真值

## 1. 为什么必须重建

旧 truth set 1 的 46 条可靠文字共 383 个规范化字符、101.19 秒语音。它们全部在 V1 的 VAD segment 上标注，46/46 的起止边界与 V1 segment 完全相同；原标注模板还包含导出时的 V1 hypothesis。它可以验证历史结果能否复现，但同时存在三种偏置：

- **选择偏置**：只有 V1 找到并切出的语音才有机会进入文字真值，V1 漏掉的语音不可见。
- **边界偏置**：参考转写沿用 V1 边界，新模型自己的 VAD、上下文和 token 边界会额外承担切分误差。
- **非盲偏置**：标注者能看到 V1 hypothesis，文字真值可能更容易沿用 V1 的解释。

因此 `30.03%` 对 `14.36%` 不是纯模型比较：前者包含 V2-C VAD、窗口、ForcedAligner 和 token 取舍，后者使用了生成这份测试集的 V1 segment。V2-C.1 不删除这些不可变审计记录，但不再据此宣称 Qwen 在真实 Watch 场景中普遍更差。

## 2. 两条互补轨道

| 轨道 | 输入边界 | 能回答的问题 | 不能回答的问题 |
| --- | --- | --- | --- |
| Oracle segmentation | 所有模型使用完全相同的人工 transcript 区间 | 在相同音频内容上，纯 ASR 文本谁更准 | 模型自己的 VAD、切分、连续长音频能力；对独立样本的泛化 |
| Blind continuous end-to-end | 未参考任何模型输出而抽取的连续范围，穷尽标注语音与文字 | 从原音到语音检出、切分和文字的整链路质量 | 单独定位某个组件贡献，需要结合 Oracle 轨道诊断 |

两个轨道必须同时保留。Oracle 轨道不能替代盲测，盲测也不能解释误差究竟来自 ASR 还是切分。

## 3. 独立盲标任务

命令只使用 session duration、不可变 input fingerprint 和显式 seed 选择一个连续范围，不读取 VAD、segment、ASR hypothesis、speaker 或 disagreement：

```powershell
allday-asr benchmark init-blind 1 `
  --name watch-blind-30m-v2c1-20260828 `
  --duration 30:00 `
  --chunk 5:00 `
  --seed v2c1-primary-20260828
```

当前真实任务固定为 session `1` 的 `6102000–7902000 ms`，即 `01:41:42–02:11:42`。30 分钟连续范围被解码为六个 5 分钟、16 kHz mono PCM 试听文件；分块只方便标注，不改变真值的连续 scope。任务位于私有目录：

```text
state/evaluations/session-000001/
  watch-blind-30m-v2c1-20260828-blind-v2c1/
```

`truth-draft.jsonl` 没有任何模型文字，初始状态为 `pending`。导入器会拒绝未完成任务，并强制检查：

- 六个窗口无缝覆盖完整 scope，全部 `review_status=complete`；
- 每个派生 WAV 的 SHA-256 与任务清单一致；
- `vad=exhaustive` 且 `transcript=exhaustive`；
- 标注者填写姓名/代号、完成时间，并确认 `model_outputs_unseen=true`；
- 文件中不存在 `segment_id`、`legacy_segment_id`、hypothesis、prediction 或 model output 字段；
- 标注和 review-region 都能重新解析到同一个不可变原音对象及 SHA-256。

所有可听清语音都应有 transcript；确实无法可靠听写的语音使用 `uncertain` + `label=unintelligible` 显式屏蔽。穷尽真值下，与任何参考 transcript 都不重叠的模型文字会作为 insertion 计入 CER；稀疏旧真值则继续忽略孤立 hypothesis，避免反向制造偏差。

人工标注完成前不能运行最终盲测，也不能用模型预填后再称为盲标。原始 Watch M4A 始终只读；WAV 是可删除、可由 source 时间范围重建的派生文件。

## 4. 同边界纯 ASR

三种 backend 都直接读取同一批人工 transcript 区间。SenseVoice 不运行 FSMN-VAD；Qwen 不运行 FSMN-VAD 和 ForcedAligner；Fun-ASR-Nano 不挂载 VAD wrapper。参考文字不会传给模型。

```powershell
allday-asr benchmark oracle-asr 1 --model sensevoice `
  --name v2c1-oracle-sensevoice-20260828 --device cuda:0
allday-asr benchmark oracle-asr 1 --model qwen `
  --name v2c1-oracle-qwen3-asr-1.7b-20260828 --device cuda:0
allday-asr benchmark oracle-asr 1 --model fun `
  --name v2c1-oracle-fun-asr-nano-20260828 --device cuda:0
```

真实库生成 prediction set `4/5/6`。每条 prediction 使用相同的人工起止时间，metadata 记录 truth annotation key、model checkpoint、参数、backend，以及原始响应哈希；内容冻结后不能修改。

## 5. 双 CER 与不确定性

报告同时给出：

- **原始规范化 CER**：NFKC、小写、删除空白与标点。
- **ITN 等价 CER**：在上述规则上，只把语法明确的中文基数词折叠为阿拉伯数字，例如 `三十五` 与 `35` 等价；`十分好`、`二零二六年` 等可能改变语义的形式不会转换。

同边界结果如下。它们仍来自 V1 条件化、非盲、仅 101.19 秒的旧样本，所以是诊断结果，不是最终晋级结果。

| 模型 | Prediction set | 原始 CER | ITN 等价 CER |
| --- | ---: | ---: | ---: |
| SenseVoiceSmall（重新推理） | 4 | 15.93% | 15.93% |
| Qwen3-ASR-1.7B | 5 | 20.63% | 19.84% |
| Fun-ASR-Nano-2512 | 6 | 25.85% | 24.80% |

Qwen 相对 SenseVoice：

- 原始 CER 差值 `+4.70` 个百分点；20 万次 paired bootstrap 95% 区间 `[0.00, +10.39]`，Qwen 更好的概率 `1.97%`。
- ITN 等价 CER 差值 `+3.92` 个百分点；95% 区间 `[-0.85, +9.56]`，Qwen 更好的概率 `4.82%`。
- 46 条区间中 SenseVoice 更好 17 条、Qwen 更好 10 条、相同 19 条（ITN 口径）。

Fun-ASR 相对 SenseVoice 的原始 CER 差值为 `+9.92` 个百分点，95% 区间 `[+3.57, +16.26]`。

统计命令：

```powershell
allday-asr benchmark compare-oracle-pair 1 4 5 --samples 200000
allday-asr benchmark compare-oracle-pair 1 4 5 --samples 200000 --itn
```

paired bootstrap 以人工 transcript 区间为成对重采样单位，固定随机种子并报告 candidate-minus-baseline 差值，不用两个独立总体的误差条代替配对比较。

## 6. 当前决策

现在可以确定的是：

1. `30.03% vs 14.36%` 不能用于纯 ASR 模型排名。
2. 在同一批 V1 条件化区间上重新推理后，SenseVoice 的点估计仍优于 Qwen 和 Fun-ASR；Qwen 的 ITN 置信区间跨过零。
3. 这份小而有偏的样本不足以证明 SenseVoice 在全天连续 Watch 音频上普遍更好，也不足以支持 Qwen 晋级。
4. 生产默认暂不改变。最终选择由完成后的 30 分钟独立盲测决定；至少同时查看原始/ITN CER、VAD miss/false alarm、场景分层和 paired uncertainty。

在盲标完成前，模型、VAD、对齐或融合策略都可以继续作为候选运行，但不得查看盲标任务后反向调参，也不得用该 30 分钟同时做开发集和最终测试集。
