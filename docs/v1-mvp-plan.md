# AllDayRecording-ASR 第一版 MVP 计划

> 状态：离线原型已跑通，处于质量评测与流程固化阶段  
> 更新日期：2026-08-28  
> 第一版目标：将华为 Watch 导出的全天录音，离线处理为可检索、可回听、区分说话人的个人当天时间线。

本文已经冻结为 V1 的历史范围与验收基线，不再决定后续技术路线。实际进度见 [项目现状与路线图](project-status.md)，下一阶段实施以 [V2 质量优先架构与实施设计](v2-quality-first-architecture.md) 为准。

## 1. 第一版要解决的问题

用户每天佩戴录音设备，结束后获得一份较长的 M4A 录音。系统需要在本地自动完成：

1. 识别真正包含语音的时间段，跳过长静音。
2. 将语音转成带时间戳的文字。
3. 区分不同说话人，并尽可能标出哪些内容由用户本人说出。
4. 将相邻对话整理成一天的时间线。
5. 让用户可以从任意文字跳回原始音频核对。

第一版的核心不是生成漂亮总结，而是建立一条结果可靠、可以追溯、可以重复运行的音频处理链路。

## 2. 第一版明确不做的内容

以下能力延后，不能阻塞第一版交付：

- Watch 到电脑的实时音频传输。
- 实时字幕和实时说话人识别。
- 自动创建系统日历、待办或发送消息。
- 未经用户选择和对方知情同意，自动记住并命名所有熟人的声纹。
- 完整桌面端或手机端产品 UI。
- 云端同步和多人账号系统。
- 复杂的情绪分析、健康分析和行为推断。

第一版可以保留相应接口和字段，但不实现上述产品功能。

目前已额外实现一个受控的本地人物样本库：可以登记本人，也可以在明确同意后登记固定人物；但跨天自动匹配固定人物仍不属于已完成功能。

## 3. 已有测试条件

测试文件：`data/watch_1787564356920.m4a`

| 项目 | 当前值 |
| --- | --- |
| 来源 | 华为 Watch / OpenHarmony 6.1 |
| 格式 | AAC in M4A |
| 采样率 | 16 kHz |
| 声道 | 单声道 |
| 时长 | 2 小时 44 分 35 秒 |
| 文件大小 | 约 79.8 MB |
| 平均码率 | 约 64 kbps |
| 简单静音估算 | 约 76%，仅作为 VAD 必要性的参考 |
| GPU | RTX 5070 Laptop GPU，8 GB 显存 |

注意：当前测试音频的静音比例是基于能量阈值的粗略统计，不等同于真实 VAD 结果。

## 4. 推荐技术路线

```text
原始 M4A
   ↓
录音入库与元数据解析
   ↓
VAD 检测和语音切段
   ↓
ASR 转写、标点和时间戳
   ↓
多人说话人分离
   ↓
保守质量过滤
   ↓
本人声纹候选 + 人工确认
   ↓
人物样本库与阈值校准
   ↓
对话事件聚合
   ↓
当天时间线 + 文本检索 + 原音回听
```

### 4.1 首选模型组合

为了先降低集成复杂度，第一条可运行链路采用：

- VAD：FSMN-VAD。
- ASR：SenseVoiceSmall。
- 说话人处理：FunASR 管线配合 CAM++。
- 本人声纹：CAM++，后续可对比 ERes2NetV2。
- 标点：优先使用 ASR 管线结果；效果不足时再增加 CT-Punc。
- 时间线整理：V1 使用确定性规则聚合；后续语义模型方案已经由 V2 的可替换云端接口取代。

### 4.2 后续基准模型

首条链路跑通后，再对同一批人工标注片段评测：

- Fun-ASR-Nano-2512：重点评测复杂中文、方言、专有名词和上下文。
- Paraformer：重点评测中文时间戳、热词和后续流式能力。
- Whisper large-v3-turbo + faster-whisper INT8：作为中英混说和跨语言对照。

模型必须通过真实 Watch 音频评测决定，不能只依据公开榜单或干净演示音频决定。

## 5. 功能需求

### 5.1 录音入库

系统接收一个或多个 M4A 文件，并保存：

- 文件路径和文件哈希。
- 设备来源。
- 录制开始时间、时区和持续时间。
- 编码格式、采样率、声道和码率。
- 处理状态、模型版本和错误信息。

重复导入同一文件时，不应重复创建处理任务。

### 5.2 音频预处理

- 原始 M4A 永久保持不变。
- 内部处理统一使用 16 kHz、单声道 PCM 数据。
- 使用 VAD 过滤静音和非语音区间。
- 为每个语音片段保留相对时间和绝对时间。
- 长录音必须分块处理，支持失败重试和断点续跑。
- 分块边界需要保留少量重叠，避免截断句子。

### 5.3 语音转写

每个稳定语音片段至少输出：

- 开始和结束时间。
- 原始识别文字。
- 清理标点后的显示文字。
- 语言。
- ASR 模型及版本。
- 置信信息；模型无法提供时允许为空。
- 对应原音频区间。

第一版优先保证片段时间戳可靠，不强制实现逐字时间戳。

### 5.4 多人说话人分离

系统需要为同一段录音中的语音分配临时标签，例如：

- `speaker_00`
- `speaker_01`
- `speaker_02`

这些标签只代表同一录音内的说话人聚类，不代表真实姓名，也不保证跨天一致。

首版采用保守质量门槛：短于 1.5 秒的语音不用于身份聚类；一个候选聚类至少需要
3 条可靠语音、合计 5 秒以上，否则相关片段输出为 `unknown`。这些数值是初始值，
后续必须通过人工标注集校准。电视、多媒体外放和多人重叠场景不得假设聚类标签等于
真实人物。

第一版必须允许出现：

- `me`：高置信度判断为用户本人。
- `speaker_xx`：尚未识别身份的说话人。
- `unknown`：语音过短、重叠或质量不足，不能可靠聚类。

系统不能为了让结果看起来完整而强制给未知说话人匹配身份。

未经逐段人工确认纯度的匿名聚类，不得直接生成本人或其他人的声纹档案。

### 5.5 本人声纹登记

用户应提供多个环境下的本人录音，总有效语音建议为 2～5 分钟，包括：

- 安静近讲。
- Watch 正常佩戴距离。
- 室外或餐厅噪声。
- 正常说话和较轻声说话。

本人识别以这些独立登记录音为主；全天录音中的无监督聚类只用于辅助定位候选片段，
不能代替本人声纹登记。匹配得分不足、片段过短、多人重叠或疑似电视声音时均保持
`unknown`。

系统为这些录音生成多个声纹 embedding，并保存声纹档案版本。真实测试已经证明匿名聚类可能混入多人和电视，因此匹配以独立档案对多个合格语音窗口的得分为主；聚类中心只能作为辅助信息，不能替代逐片段质量控制。

阈值必须可配置，并保留全部匹配得分和人工真值用于校准。当前 `0.36` 只是基于少量人工样本的实验值，必须通过跨日期留出集重新评测。

#### 5.5.1 人物声纹样本库与日常积累

人物样本库采用本地、人工确认优先的分层结构：

- `accepted`：2～8 秒、单人、无电视和重叠的确认片段，可更新活动声纹。
- `holdout`：按整次会话隔离的验证片段，永不反向更新声纹。
- `negative`：电视、其他人物、混合说话和不确定片段，只用于阈值校准。
- `enrollment_source`：独立登记的原始录音，以路径和 SHA-256 引用，不修改原件。

积累时必须遵守：

- 同一次会话不能同时拆入 `accepted` 和 `holdout`。
- 每次会话最多吸收 60 秒 `accepted` 人物语音。
- 未经人工确认的自动预测不得反向更新人物档案。
- 活动档案最多保留 100 个有代表性的 embedding，优先覆盖不同日期和环境。
- 有效语音不足 2 分钟时为 `provisional`；达到 2 分钟和 10 个 embedding 后为
  `active`；达到 5 分钟、5 次独立会话和 30 个 embedding 后为 `stable`。
- 陌生人默认只使用会话级匿名标签；只有用户明确选择且对方同意时，才建立长期
  `known_person` 声纹。

### 5.6 对话事件聚合

相邻语音片段按以下信号聚合为一次对话事件：

- 时间间隔。
- 说话人连续性。
- 是否存在较长静音。
- 文本是否明显延续上一句。

初始可使用规则，例如：连续片段间隔不超过 2 分钟时归为同一候选事件；最终阈值必须通过测试音频调整。

### 5.7 当天时间线

第一版至少输出两种形式：

1. 机器可读的 JSON/JSONL。
2. 供用户阅读的 Markdown 或 HTML 时间线。

示例：

```markdown
## 2026-08-24

### 20:35–20:42 对话

- 未知说话人 1：明天十点饭店见。
- 我：好的。
- [回听原音 00:12:31–00:12:38]
```

第一版的时间线只能整理已识别事实，不应编造没有出现在录音里的活动。

### 5.8 原音回听

任何转写片段都需要保存原始录音文件、开始时间和结束时间。即使第一版没有完整 GUI，也应提供一个命令或本地接口，可以截取或播放对应区间。

## 6. 建议数据结构

第一版使用 SQLite 即可，不需要引入复杂数据库。

### recordings

```text
id
source_path
sha256
device
recorded_at
timezone
duration_ms
codec
sample_rate
channels
status
created_at
```

### speech_segments

```text
id
recording_id
start_ms
end_ms
speaker_session_id
person_id nullable
speaker_match_score nullable
language nullable
text_raw
text_display
asr_model
audio_ref
```

### person_profiles

```text
id
display_name
profile_type: self | known_person
embedding_model
embedding_version
created_at
```

声纹向量可以单独加密保存，不建议直接以可读 JSON 存入仓库。

### conversation_events

```text
id
recording_id
start_ms
end_ms
title nullable
summary nullable
segment_ids
```

### segment_identity_annotations

```text
recording_id
segment_id
identity_label
raw_label
confidence
annotation_source
note
```

### voice_library_samples

```text
sample_key
person_id nullable
identity_label
sample_type
split: accepted | holdout | negative
source_path
recording_id nullable
segment_id nullable
session_key
duration_ms
speech_ms
embedding nullable
embedding_model/version
human_confirmed
metadata
```

## 7. 推荐项目结构

```text
AllDayRecording-ASR/
├─ data/                    # 本地测试音频，不提交私人数据
├─ docs/                    # 设计和测试说明
├─ src/allday_asr/
│  ├─ asr/                  # FunASR 模型适配
│  ├─ audio/                # FFmpeg、标准化和音频片段
│  ├─ services/             # 入库、处理、说话人、审核、样本库、时间线
│  ├─ storage/              # SQLite 数据结构与访问
│  ├─ cli.py                # Typer 命令入口
│  └─ paths.py              # 本地数据路径
├─ tests/
├─ outputs/                 # 本地运行结果，不提交私人数据
├─ models/                  # 本地模型缓存，不提交仓库
└─ pyproject.toml
```

## 8. 命令行接口目标

当前已经提供以下主命令：

```powershell
# 检查环境和 GPU
allday-asr doctor

# 校验统一配置和数据库 schema 版本
allday-asr config-show

# 导入录音
allday-asr ingest data/watch_1787564356920.m4a

# 运行完整离线处理
allday-asr process <recording-id>

# 推荐：安全、幂等地编排完整离线日记
allday-asr daily-run <audio-path-or-recording-id>

# 匿名说话人和质量过滤
allday-asr diarize <recording-id>

# 登记本人声纹
allday-asr enroll-self <audio-or-directory>

# 生成候选，人工修改候选 README 后导入
allday-asr self-candidates <recording-id>
allday-asr import-self-review <recording-id>

# 同步/积累/查看人物样本库
allday-asr voice-library sync
allday-asr voice-library accumulate <recording-id>
allday-asr voice-library status

# 生成录音级事件时间线
allday-asr timeline <recording-id>

# 导出机器可读结果
allday-asr export <recording-id> --format jsonl

# 创建人工真值并生成评测报告
allday-asr evaluation init <recording-id> --end 15:00
allday-asr evaluation run <truth.jsonl>
```

所有长任务都需要显示当前阶段、处理时长、实时倍率和错误原因。

## 9. 开发阶段和完成条件

### M0：环境自检（已完成）

- 创建独立 Python 环境。
- 安装支持 RTX 50 系列的 CUDA 12.8 PyTorch。
- `torch.cuda` 可以真正执行矩阵运算，而不只是返回 `True`。
- FFmpeg/FFprobe 可用。
- `doctor` 能报告 Python、PyTorch、CUDA、GPU、显存和模型依赖状态。

### M1：录音入库与 VAD（已完成）

- 能导入测试 M4A。
- 正确读取录制时间和音频参数。
- 能输出语音段清单。
- 重复运行不会重复导入。
- 处理可以中断后继续。

### M2：单人 ASR 基线（已完成）

- 所有 VAD 片段能完成转写。
- 输出带时间戳的 JSONL。
- 可以从文字定位回原音频。
- 记录耗时、实时倍率和显存峰值。

### M3：多人分离与本人识别（原型完成，质量待评测）

- 输出匿名 speaker 标签。
- 完成本人声纹登记。
- 高置信度片段标记为 `me`。
- 低置信度片段保持未知。
- 人工抽查时可以查看匹配分数和对应原音。
- 已增加人工标注导入、留出/负样本库和固定人物受控登记。
- 尚未达到跨场景说话人准确率验收，也未实现固定人物跨天自动匹配。

### M4：当天时间线（规则版已完成）

- 将相邻片段聚合成对话事件。
- 生成 Markdown 和 JSON 时间线。
- 每条文字都能追溯至原始录音区间。
- 一次完整运行不会修改原始音频。

当前时间线完成的是片段聚合和证据追溯，尚未完成“我今天做了什么”的语义摘要、日程和待办候选。

## 10. 第一版验收标准

### 必须满足

- 完整处理现有 2 小时 44 分测试音频，不因长度崩溃。
- 过滤大部分长静音，ASR 不处理整段静音音频。
- 所有转写都有可靠的片段级时间戳。
- 支持至少两名说话人的匿名区分。
- 可以标出部分高置信度的本人语音。
- 所有结果可以回听原音验证。
- 处理结果可重复生成，且模型和参数有版本记录。
- 任务失败时保留已完成结果，可以续跑。

### 第一版不设为硬指标

- 不要求所有人都被正确命名。
- 不要求逐字时间戳。
- 不要求完全准确处理多人重叠讲话。
- 不要求自动生成或写入日程。
- 不要求低于一秒的实时延迟。

### 当前验收结论（2026-08-28）

- 2 小时 44 分测试录音已完整处理，423 个 VAD 片段全部完成 ASR。
- 片段时间戳、原音截取、JSONL/Markdown 和事件时间线已经跑通。
- 本人独立声纹、候选人工审核和样本库积累已经跑通。
- 匿名多人聚类在电视、短句和混合语音上尚未达到可靠身份识别标准；当前用 `unknown` 和人工确认控制风险。
- 首个连续 15 分钟人工真值和正式报告已经完成，但只有 46 段能可靠听写、本人正样本仅 2 段，V1 仍需扩充跨场景评测并改进电视/短句切分。

## 11. 模型评测方法

从测试音频中选取 15～30 分钟建立人工标注集，覆盖安静、噪声、远场、多说话人、人名、时间地点和中英混说。

至少记录：

- 中文字符错误率 CER。
- 时间、地点、人名等关键信息准确率。
- 说话人分离错误率或人工错误计数。
- 本人声纹的误接受和误拒绝情况。
- 处理实时倍率 RTF。
- GPU 显存峰值。
- 失败片段数量。

模型选择以“真实 Watch 录音中的关键事实是否正确”为最高优先级，而不是只比较文本是否读起来流畅。

## 12. 隐私与数据安全底线

- 私人原始音频、转写、声纹和数据库默认不得提交 Git。
- 原始音频只能读取，不得在处理流程中覆盖。
- 输出目录、模型目录和数据库需要纳入 `.gitignore`。
- 声纹 embedding 视为敏感数据，支持删除和重新登记。
- 所有自动总结都要保留证据片段，允许用户核对和纠正。
- 在实际持续录制他人之前，应确认当地规则及适当的告知、同意要求。

## 13. 第一版完成后的下一步

V1 已完成原型闭环。2026-08-28 起，后续工作转入 [V2 质量优先架构](v2-quality-first-architecture.md)：不可变原始对象、schema v5、连续时间真值和多 run benchmark 已完成，下一步接入高质量 ASR、强制对齐、重叠感知说话人时间轴和云端语义接口。Watch 同步延后，不阻塞当前长录音开发；实时字幕不再是既定交付目标。
