# 人工真值与评测指南

## 目标

评测的目的不是得到一个好看的总分，而是回答四个产品问题：

1. Watch 录音里的文字到底错了多少？
2. 系统能否把同一个人的发言聚在一起，又不会把不同人混在一起？
3. 系统判断“这是我说的”时，误认和漏认各有多少？
4. 日期、时间、地点、人名和动作等关键事实有没有被正确保留？

## 1. 创建连续评测范围

建议第一份真值覆盖连续 15 分钟，之后逐步补到 30 分钟，包含安静、远场、电视、多人和短句。

```powershell
allday-asr evaluation init 1 `
  --name baseline-first-15m `
  --start 0 `
  --end 15:00
```

真值默认写入：

```text
state/evaluations/recording-000001/baseline-first-15m.jsonl
```

文件位于私有 `state/`，不会提交 Git。同名文件默认拒绝覆盖；只有明确添加 `--force` 才会重建。

## 2. 填写字段

每个 `type=segment` 的 JSON 对象填写：

- `reference_text`：人工逐字听写。空值表示本段不参与 CER。
- `reference_speaker`：同一人物始终用同一标签，例如 `self`、`mother`、`father`、`teacher`、`tv`。
- `reference_identity`：只填写 `self` 或 `not_self`；混合、无法判断时留空。
- `key_facts`：本句必须识别对的事实，例如 `["明天十点", "饭店"]`。
- `include`：音频损坏、严重重叠且无法形成真值时设为 `false`。
- `notes`：记录电视、重叠、方言、远场等场景。

不要修改 `segment_id`、`start_ms`、`end_ms`。`hypothesis_*_at_export` 只是创建模板时的系统结果；正式评测总是读取数据库中的当前预测。

需要回听时：

```powershell
allday-asr clip <segment-id>
```

为避免被现有转写诱导，建议先听音频写 `reference_text`，再对照 `hypothesis_text_at_export`。

## 3. 运行评测

```powershell
allday-asr evaluation run `
  state\evaluations\recording-000001\baseline-first-15m.jsonl
```

每次运行都会：

- 计算去空格和标点后的中文 CER。
- 计算不依赖 `speaker_00` 标签名称的说话人成对 precision、recall、F1 和准确率。
- 计算本人识别 TP、FP、FN、TN、precision、recall 和 accuracy。
- 检查 `key_facts` 是否出现在当前识别文字中并计算召回率。
- 保存真值 SHA-256、评测配置、逐片段明细和报告路径到 SQLite。

没有填写相应真值的指标显示为 `N/A`，不会被计为 0。

## 4. 比较实验

第一次完整填写后不要再随意修改真值。把它作为 baseline：

1. 运行当前模型并保存报告。
2. 每次只改变一类因素，例如 VAD 边界、说话人切分或 ASR 模型。
3. 使用同一真值文件和 SHA-256 再次评测。
4. 除总指标外，检查逐片段报告，确认关键时间地点没有退化。

当前最重要的改进目标是降低不同人物和电视被混为同一 speaker 的情况，同时保持本人误接受率足够低。
