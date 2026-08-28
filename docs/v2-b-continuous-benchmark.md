# V2-B 连续时间真值与 Benchmark 指南

V2-B 将评测事实从任何一次 VAD/ASR 的 `segment_id` 中解耦。真值和预测都绑定同一个不可变 `recording_session` 输入指纹，时间使用会话毫秒坐标；每条冻结真值在数据库中进一步展开为原始对象 SHA-256 和源内时间范围。

## 1. 已有 V1 标注迁移

迁移不会修改或覆盖旧 JSONL：

```powershell
allday-asr benchmark migrate-v1-truth `
  state\evaluations\recording-000001\baseline-first-15m.jsonl `
  --name baseline-first-15m-continuous-v2
```

然后冻结当前 V1 输出并运行基准：

```powershell
allday-asr benchmark snapshot-v1 1 --name v1-sensevoice-current-20260828
allday-asr benchmark run 1 1
allday-asr benchmark compare 1
```

`truth_set`、`prediction_set` 和 benchmark run 都是只追加对象。冻结后数据库拒绝新增、修改或删除其内容；模型或参数改变时创建新的预测快照，不能覆盖旧结果。

当前 15 分钟标注迁移得到 277 条事实：87 个稀疏 speech 区间、46 条文字、72 条说话人和 72 条本人/非本人身份标注。V1 快照包含 971 条预测，连续时间 CER 为 `0.1436`，与原报告一致。

## 2. 为什么当前 VAD 和 DER 显示 N/A

旧模板由 V1 VAD segments 生成，人工只检查了模型已经发现的区域。它无法证明两个 segment 之间没有漏掉语音，也没有逐帧标完整的说话人活动。因此迁移元数据明确标为：

- `vad = sparse_positive_only`
- `speaker = sparse_segment_labels`
- `alignment = none`

在这种覆盖下，系统拒绝计算 VAD Miss/False Alarm、DER/JER 和 token 对齐误差。把未标区域直接当成非语音会系统性美化 VAD，V2-B 不允许这样做。

## 3. 创建新的连续真值

先生成只包含会话输入指纹和范围的模板：

```powershell
allday-asr benchmark init-truth 1 `
  --name first-15m-exhaustive `
  --start 0 `
  --end 15:00
```

在 metadata 后添加任意数量的 annotation。annotation 不需要沿用模型切分，也可以相互重叠：

```json
{"type":"annotation","key":"speech:0001","kind":"speech","session_start_ms":53120,"session_end_ms":54240,"label":"speech","text":null,"metadata":{"reviewed":true}}
{"type":"annotation","key":"speaker:mother:0001","kind":"speaker","session_start_ms":53120,"session_end_ms":53800,"label":"mother","text":null,"metadata":{"reviewed":true}}
{"type":"annotation","key":"speaker:self:0001","kind":"speaker","session_start_ms":53600,"session_end_ms":54240,"label":"self","text":null,"metadata":{"reviewed":true}}
{"type":"annotation","key":"text:0001","kind":"transcript","session_start_ms":53120,"session_end_ms":54240,"label":null,"text":"准确听写内容","metadata":{"reviewed":true}}
{"type":"annotation","key":"token:0001","kind":"alignment_token","session_start_ms":53120,"session_end_ms":53310,"label":null,"text":"准","metadata":{"token_index":0}}
{"type":"annotation","key":"entity:time:0001","kind":"entity","session_start_ms":53120,"session_end_ms":54240,"label":"time","text":"明天十点","metadata":{"reviewed":true}}
```

同一时刻存在两条 `speaker` annotation 就表示重叠讲话。导入器会根据 session 时间自动生成并校验 `source_object_id + SHA-256 + source_start_ms/source_end_ms`，包括跨未来 5 分钟块的标注。

完成后，根据实际人工覆盖修改 metadata 的 `completeness` 并导入：

```powershell
allday-asr benchmark import-truth `
  state\evaluations\session-000001\first-15m-exhaustive-continuous-v2.jsonl
```

只有整段范围逐时间检查完成后，才允许把相应任务设为 `exhaustive`：

| 字段 | `exhaustive` 的含义 |
| --- | --- |
| `vad` | 整个 scope 已检查，全部语音正区间已标；其余时间可作为非语音 |
| `speaker` | 全部说话人活动已标，包含换人、静音和重叠讲话 |
| `alignment` | 参与评测的全部 reference token 都有人工时间边界 |
| `transcript` | 整个 scope 的可听清语音均有文字；稀疏文字仍可计算局部 CER |
| `entities` | 目标实体类别已穷尽标注，可计算完整 Precision/Recall/F1 |

## 4. 指标定义

- ASR：按连续真值区间聚合所有相交预测，计算规范化 CER 和完全匹配率，不要求预测切分与真值相同。
- VAD：在完整 scope 的原子时间区间上计算 Miss、False Alarm、precision、recall、F1 和平均边界误差。
- 说话人：先做标签置换最优匹配，再计算包含重叠讲话的 DER、重叠子集 DER 和 JER。
- 对齐：按 token 文本、可选 `token_index` 和时间邻近关系匹配，报告失败率、平均起止误差和 P95 边界误差。
- 实体：显式 entity 输出计算 Precision/Recall/F1；另报告参考实体是否被转写文字保留的召回率。

报告始终保存真值 SHA-256、预测内容 SHA-256、原始输入指纹、评测配置和不可用原因。同一个 truth set 可以比较任意多个预测快照；输入指纹不同的组合会被拒绝。
