# 人工真值与评测指南

> 本文记录 V1 `segment_id` 兼容流程。新的模型比较应使用 [V2-B 连续时间真值与 Benchmark 指南](v2-b-continuous-benchmark.md)；V1 JSONL 可以无损迁移，原文件不会被覆盖。

## 目标

评测的目的不是得到一个好看的总分，而是回答四个产品问题：

1. Watch 录音里的文字到底错了多少？
2. 系统能否把同一个人的发言聚在一起，又不会把不同人混在一起？
3. 系统判断“这是我说的”时，误认和漏认各有多少？
4. 日期、时间、地点、人名和动作等关键事实有没有被正确保留？

这份真值是留出的测试集，不直接用于训练、调阈值或扩充声纹样本库。否则模型等于提前看过答案，后续指标会失去可信度。

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

重叠或混合讲话无法对应到一个人物时，`reference_speaker` 和 `reference_identity` 都留空，并在 `notes` 中分别记录听到的人。不要把所有混合片段都标成同一个名为 `mixed` 的人物，否则会扭曲说话人成对指标。纯吃饭声、碰撞声等非语音片段设为 `include=false`。

不要修改 `segment_id`、`start_ms`、`end_ms`。`hypothesis_*_at_export` 只是创建模板时的系统结果；正式评测总是读取数据库中的当前预测。

需要回听时：

```powershell
allday-asr clip <segment-id>
```

为避免被现有转写诱导，建议先听音频写 `reference_text`，再对照 `hypothesis_text_at_export`。

网页工作台为每段提供两个播放器：

- “评测片段”严格使用原片段时间边界，`reference_text` 只能抄这里听到的内容。
- “辅助上下文”额外播放前后各 3 秒，只用于判断场景、人物和被截断词语，不把片段外文字写进 `reference_text`。

两个试听副本都会自动提升响度；原录音、片段边界和模型输入不会因此改变。

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


## V3 人工确认收益评估（2026-09-24）

这是固定真实人物身份的前后对照，不是匿名聚类指标。先复制本地 V3 数据库和声纹状态，保留原版本用于 baseline；在另一副本只对建库集人工确认并采纳样本。固定模型、模型版本、阈值、ASR 产物、处理条件和同一测试证据，使用现有匹配入口运行前后两次，导出真实预测。不在正式数据库中撤销全部真实标注来构造 baseline。

由用户确认人物真值。建库集和测试集按日期隔离；同一真实对话的手机、手表及其他设备录音使用同一 conversation_id，绝不能跨集合。陌生人也使用稳定真值 ID，但不放进 known_person_ids。未知预测写 null，不能删掉难例。评估测试集不得标人物、采纳样本或用于调阈值。

新增纯本地统计入口（它不替你运行声音模型，也不生成真值）：

```powershell
.\.venv\Scripts\python.exe tools/evaluate_annotation_benefit.py state/evaluations/benefit-manifest.json state/evaluations/benefit-predictions.jsonl
```

manifest 模板：

```json
{
  "model": "实际模型",
  "model_version": "实际固定版本",
  "conditions": {"policy_snapshot": "本地策略快照路径/摘要", "asr_run": "固定处理运行"},
  "self_person_id": "self-person-id",
  "known_person_ids": ["self-person-id", "familiar-person-id"],
  "review_minutes": 0,
  "enrollment": [{"evidence_id": "建库原音范围 ID", "conversation_id": "对话 A", "date": "2026-09-01"}]
}
```

predictions 每行模板（仅格式示例，不能作为实测结果）：

```json
{"evidence_id":"测试原音范围 ID","conversation_id":"独立对话 B","date":"2026-09-02","truth_person_id":"familiar-person-id","before":null,"after":null,"before_review_requests":0,"after_review_requests":0}
```

下游若已逐条核对，可补充 before_reminder_correct / after_reminder_correct、before_event_correct / after_event_correct、before_memory_correct / after_memory_correct 布尔字段。没有核对就省略，输出 evaluated=0，不能按正确计数。review_requests 统计同一固定测试范围实际要求人工介入的次数，不能用候选条数冒充。

结果至少保留：本人及每位熟人的样本数/正确数、已知人物留未知率、陌生人误认熟人率、提醒/事件/记忆的实测正确数及分母、审核次数、人工分钟数、净改善与退步。分母为零输出 null；只做到“谁都不认”会显示已知留未知率升高，而非被包装为成功。

当前结果：未运行真实前后对照。旧评测 JSONL 没有对话隔离标识与稳定人物真值，需先补齐。主机 fixture 验证只证明统计公式、数据隔离检查和软件匹配调用链，不证明声音效果。真机另外记录设备型号、系统版本、样本量、离线重启同步结果和审核页响应时间。
