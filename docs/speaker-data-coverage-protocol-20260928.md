# Speaker Data Coverage Audit：可公开协议

本协议只定义数据口径。带有人物姓名、人物 ID、session、媒体定位和逐事件范围的审计结果保存在被 `.gitignore` 排除的 `outputs/speaker-data-coverage-audit-20260928/`。

## 数据来源与真值

工具运行时检查正式 SQLite 的实际列结构，以 `mode=ro`、`query_only=ON` 和一个读事务读取 `annotation_facts`、`annotation_fact_audio`、`utterances`、`recording_sessions`、`speaker_tracks`、`voice_prototypes`、`annotation_sample_sets`、`persons`、`audio_assets`、`audio_replicas`、`capture_segments`。人物真值只计 `active` 的 `legacy-human`、`phone-operation:*`、`desktop-operation:*` 人物 fact。系统身份事实和自动人物预测不计入。`annotation_count` 是不同 fact 的数量；`annotated_utterance_count` 是不同来源 utterance 的数量。时长按同一原音 SHA 上的标注范围并集计算，避免重叠重复计时。

`speaker_track_count` 指来源 utterance 的原始轨；人工逐句改标可能创建大量新 track，另记 `annotation_track_count`，不视为独立音频证据。prototype 和 sample set 只作链路画像，不增加人工真值数。

## Independent audio event

先以人物、session 和 utterance 在 session 内的时间位置排序。相邻范围间隔不大于指定阈值时链式合并；同一原音 SHA 的重叠范围即使跨 session 也合并。记录事件中的原始 speaker track 和当前 cluster 作为相关性诊断；人工改标新建的 track 和自动 cluster 不作为切分边界，因为它们可能由单句标注或算法生成。主口径 strict 使用 15 分钟，relaxed 使用 2 分钟，同时公布 30 秒、2 分钟、5 分钟、15 分钟敏感性。15 分钟是保守的连续生活场景上界；2 分钟允许较远的对话另计。阈值由实际间隔分布和敏感性支持，不代表录音环境变化已被验证。

`usable_independent_event_count` 只表示事件有至少 6 秒去重人物标注范围，且数据库把对应原音副本标为 available；这不是重听确认、单人纯净语音、VAD/SNR 合格或模型可评分的保证。即使两个事件跨日期，也不能自动推断为跨设备或跨环境。人物事件可能共处同一 session，所以人物事件数之和也不是全局独立录音数。

## 累计与盲测

累计曲线按录制时刻排序；“最近 500 条新增”按 fact 创建时刻排序，两者回答不同问题。旧数据导入会扭曲 fact 创建时序，因此报告同时列出录制顺序尾部 500 条。新盲测必须按冻结时点和完整 session / 原音来源核查，不能从旧数据随机抽 utterance。`blind_event_count` 使用上一轮冻结验证，并核对当前 annotation revision。

复现：在仓库根目录运行 `python tools/audit_speaker_data_coverage.py`。所有逐人物输出及图均写到忽略的 `outputs/`，不会修改正式数据库或生产识别代码。
