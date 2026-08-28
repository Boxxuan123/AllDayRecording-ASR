# AllDayRecording-ASR

一个本地优先的全天录音处理原型：将华为 Watch 导出的长录音离线处理为带时间戳、可回听、可人工校正身份的文字时间线。

当前 **可评测的一键离线日记 V1** 仍可完整运行；V2-A/V2-B/V2-C 已完成不可变原音、schema v6、逻辑窗口、连续时间真值、多 run benchmark、Qwen3-ASR-1.7B 强制对齐和 Fun-ASR-Nano 第二假设。V2-C.1 已重建公平评测协议并生成独立 30 分钟盲标任务；人工盲标完成前不再用旧 V1 条件化真值作模型晋级结论。V2 不以实时性或小模型为目标；盲测之后继续实现允许重叠的说话人时间轴，再接云端 LLM 和 Watch 同步。

实施依据见 [V2 质量优先架构与实施设计](docs/v2-quality-first-architecture.md)。当前结果、风险和进度见 [项目现状与路线图](docs/project-status.md)，V2-B 操作见 [连续时间真值与 Benchmark 指南](docs/v2-b-continuous-benchmark.md)，V2-C 操作见 [质量优先双 ASR 与强制对齐](docs/v2-c-quality-asr.md)，公平性修订见 [V2-C.1 公平基准重建](docs/v2-c1-fair-benchmark.md)。

## 已验证环境

- Windows、Python 3.12。
- RTX 5070 Laptop GPU，8 GB 显存。
- PyTorch / torchaudio `2.9.1+cu128`、torchvision `0.24.1+cu128`。
- FunASR `1.4.4`、ModelScope `1.39.1`、Qwen-ASR `0.0.6`。
- FFmpeg / FFprobe 可用。

PyTorch 使用 CUDA 专用 wheel，应根据显卡和 CUDA 环境单独安装，因此不由 `pyproject.toml` 自动解析。

```powershell
# 已进入 .venv 后，安装项目本身而不重新解析 Torch
python -m pip install -e . --no-deps

# 检查 Python、FFmpeg、模型依赖和真实 CUDA 运算
allday-asr doctor
```

## V2-A：不可变原音与逻辑窗口

schema v4 会把既有长录音无损映射为 `source_object`、`recording_session` 和 `session_source`。从 schema v3 首次升级前会自动创建 SQLite 备份；原始音频不移动、不改名、不重编码。不可变证据字段受到数据库触发器保护，完整性审计只记录新结果，不会用当前文件状态覆盖首次入库的 SHA-256。

```powershell
# 查看永久原始对象和对应会话
allday-asr sources

# 只读校验 SHA-256、字节数、时长、编码、采样率和声道
allday-asr source-audit

# 为会话 1 规划 5 分钟核心窗口和两侧 5 秒上下文；只规划，不落盘 PCM
allday-asr session-windows 1 --window-seconds 300 --context-seconds 5
```

逻辑窗口可以跨越多个未来 Watch 5 分钟块；只有模型真正运行时才临时解码当前窗口，退出后立即删除。当前 V1 已有的整段标准化 WAV 保留用于兼容和复现，但 V2-A 不再创建新的长期整段 PCM。

## V2-B：连续时间真值与多 run Benchmark

schema v5 将真值、预测快照和报告都绑定到同一个不可变输入指纹。旧 `segment_id` 只作为迁移来源说明，不再是评测主键；替换 VAD segments 不会使冻结真值失效。

```powershell
# 无损迁移已有 15 分钟 V1 标注
allday-asr benchmark migrate-v1-truth `
  state\evaluations\recording-000001\baseline-first-15m.jsonl

# 冻结当前 V1 结果、运行并比较 benchmark
allday-asr benchmark snapshot-v1 1 --name v1-sensevoice-current
allday-asr benchmark run 1 1
allday-asr benchmark compare 1

# 为真正穷尽式的 VAD/DER 标注创建新模板
allday-asr benchmark init-truth 1 --name first-15m-exhaustive --end 15:00
```

当前迁移基线包含 277 条连续时间事实和 971 条冻结预测，CER 为 `14.36%`。旧标注不是穷尽式 VAD/说话人真值，因此 VAD-F1、DER/JER 显示 `N/A`，不会把未标时间错误地当成非语音。完整格式与指标定义见 [V2-B 指南](docs/v2-b-continuous-benchmark.md)。

## V2-C：最大模型双假设与逐 token 追溯

schema v6 为每个五分钟窗口不可变保存 Qwen3-ASR-1.7B 主假设、Qwen3-ForcedAligner 时间戳、Fun-ASR-Nano 第二假设和分歧队列。每个 token 同时保存 session 时间与原始 M4A 的 source 时间、对象 ID 和 SHA-256；窗口上下文 token 保留作诊断，冻结视图只取 token 中点位于 core 的内容以避免重复。

当前 8 GB RTX 5070 已完成 2 小时 44 分 35 秒录音的全量 BF16 运行：33 个主假设、33 个第二假设、6,147 个对齐 token 和 16 个分歧窗口，耗时约 8 分 14 秒且未 OOM。全部 token 的源范围覆盖检查为 0 错误。旧 token 时间快照的 `30.03%` 与 V1 的 `14.36%` 混合了切分/对齐差异，并且真值本身由 V1 segment 产生，不能再解释为纯模型排名。V2-C.1 同边界重跑为 SenseVoice `15.93%`、Qwen `20.63%`（ITN `19.84%`）、Fun-ASR `25.85%`（ITN `24.80%`）；这仍是 V1 条件化诊断集，最终晋级等待独立盲测。

```powershell
# 当前 8 GB 5070：模型精度不变，batch=1 且两套模型顺序加载
allday-asr asr-v2 run 1 --profile compatible-8gb

# 默认 quality-16gb：BF16、无量化、较高 batch
allday-asr asr-v2 run 1

# 冻结主假设并与 truth set 1 / V1 基线比较
allday-asr asr-v2 snapshot <run-id> --name qwen3-asr-1.7b-v2c
allday-asr benchmark run 1 <prediction-set-id>
allday-asr benchmark compare 1
```

完整配置、续跑语义、边界策略和依赖固定原因见 [V2-C 指南](docs/v2-c-quality-asr.md)。

## V2-C.1：公平基准

```powershell
# 只依赖原音指纹、时长和 seed 生成连续盲标范围；任务不含模型输出
allday-asr benchmark init-blind 1 --name watch-blind-30m-v2c1-20260828 `
  --duration 30:00 --chunk 5:00 --seed v2c1-primary-20260828

# 在同一人工 transcript 边界上比较纯 ASR
allday-asr benchmark oracle-asr 1 --model sensevoice --name oracle-sensevoice
allday-asr benchmark oracle-asr 1 --model qwen --name oracle-qwen
allday-asr benchmark compare-oracle-pair 1 <baseline-set> <candidate-set> `
  --samples 200000 --itn
```

盲标导入必须满足完整窗口复核、穷尽式 VAD/转写覆盖、派生音频哈希和 `model_outputs_unseen` 声明。当前真实 30 分钟任务范围为 `01:41:42–02:11:42`，状态保持 `pending`，不能在人工完成前产生最终结果。完整协议、统计口径和当前诊断结果见 [V2-C.1 指南](docs/v2-c1-fair-benchmark.md)。

## 推荐：一键离线日记

所有一键参数集中在 [allday-asr.toml](allday-asr.toml)。先校验配置和数据库版本：

```powershell
allday-asr config-show
```

可以直接传入新音频，也可以复用已经入库的 `recording_id`：

```powershell
allday-asr daily-run data\watch_new.m4a
allday-asr daily-run 1
```

`daily-run` 会安全执行 ingest → process → diarization → 本人候选 → timeline → export，并生成 `daily-run.md/json`。重复运行时：

- 复用已经完成的 ASR，不重复计算。
- 复用已有说话人标签，避免清除人工身份。
- 不覆盖已经存在的候选审核 README。
- 将最终配置、配置 SHA-256、每一步状态和结果路径写入 SQLite。
- 缺少本人声纹或需要试听时返回 `needs_attention`，但仍然生成可用日记。

## 可视化工作台

启动本地网页：

```powershell
allday-asr web
```

命令会自动打开一个带临时令牌的本机地址。网页目前可以：

- 选择已入库录音，查看转写、身份标注、事件和运行概览。
- 逐段播放原音，填写准确文字、真实说话人、本人身份与关键事实。
- 直接生成 CER、说话人成对 F1、本人识别和关键事实评测报告。
- 确认或忽略日程/待办候选；不会写入真实日历。
- 重新执行幂等的离线日记流程，并查看运行历史。

服务固定监听 `127.0.0.1`，不会对局域网开放；录音和真值不会上传。完整说明见 [本地网页工作台](docs/web-console.md)。

## 分步离线流程

```powershell
# 1. 导入并去重
allday-asr ingest data\watch_1787564356920.m4a

# 2. 标准化、VAD 和可断点续跑的 ASR
allday-asr process 1 --max-segments 3
allday-asr process 1

# 3. 生成录音内匿名说话人标签；短片段和弱聚类保持 unknown
allday-asr diarize 1

# 4. 首次使用时，从只有本人声音的独立录音建立声纹
allday-asr enroll-self data\self-voice
allday-asr voice-library sync

# 5. 只生成本人候选，不直接写入身份
allday-asr self-candidates 1 --min-segment-seconds 0.8 --threshold 0.45 --top 30

# 6. 试听并修改 outputs\recording-000001\self-candidates\README.md 后导入
allday-asr import-self-review 1 --threshold 0.36

# 7. 将本次人工真值积累到留出集/负样本库，并查看状态
allday-asr voice-library accumulate 1 --split holdout
allday-asr voice-library status

# 8. 生成事件时间线和完整转写
allday-asr timeline 1
allday-asr export 1 --format markdown
allday-asr export 1 --format jsonl
```

默认中文识别；中英混说测试可使用：

```powershell
allday-asr process 1 --language auto --reprocess-asr
```

`--reprocess-asr` 会清除并重跑派生转写，普通续跑不要加这个参数。

## 人物身份与声纹库

匿名 `speaker_XX` 只代表一次录音内的聚类，不代表固定人物。真实 Watch 场景中，一个聚类可能混有本人、家人和电视声音，因此推荐按片段审核：

- `self-candidates`：用本人声纹生成候选和试听文件，不自动确认。
- `import-self-review`：导入人工标注并应用保守阈值。
- `voice-library accumulate`：只积累人工确认的本人、其他人、电视、混合和不确定样本。
- `voice-library status`：查看有效语音、会话数、embedding、留出集和阈值。

以下命令属于辅助或兼容入口，不是常规身份流程：

```powershell
# 导出匿名聚类试听样本，仅用于分析聚类质量
allday-asr speaker-samples 1 --speaker speaker_01 --per-speaker 20

# 仅在逐段确认整个聚类纯净时才允许整簇标为本人
allday-asr mark-self 1 speaker_03 --confirmed-pure

# 撤销该录音的本人绑定，保留独立声纹档案
allday-asr unmark-self 1

# 只用于旧结果：在建立任何身份绑定前重新应用质量门槛
allday-asr audit-speakers 1
```

经对方知情同意后，可以登记固定人物的独立声纹：

```powershell
allday-asr voice-library enroll-person "妈妈" data\voice-library\mother
```

这一步目前只建立人物档案和样本库；**跨天自动识别该人物尚未实现**。日常对话无需要求所有人预先上传声纹，默认保留为会话级匿名人物。

## 人工真值与评测

从录音的一个连续时间范围生成私有 JSONL 模板：

```powershell
# 前 15 分钟；start/end 也支持 MM:SS 和 HH:MM:SS
allday-asr evaluation init 1 --name baseline-first-15m --start 0 --end 15:00
```

人工填写 `reference_text`、`reference_speaker`、`reference_identity` 和 `key_facts` 后运行：

```powershell
allday-asr evaluation run state\evaluations\recording-000001\baseline-first-15m.jsonl
```

也可以启动 `allday-asr web`，在“评测标注”页逐段试听和保存，不必手工编辑 JSONL。

报告包含 CER、说话人成对 F1、本人识别指标和关键事实召回率。真值和报告分别保存在已被 Git 忽略的 `state/` 与 `outputs/`。

## 日程/待办候选

`daily-run` 会对包含明确日期、时间和行动词的语句执行高置信规则提取。默认还要求检测到“本人提出”或随后由本人说出“好的/可以”等确认语。候选始终保留提议、确认、原音区间和匹配规则作为证据。

```powershell
# 查看待确认项
allday-asr actions 1 --status pending

# 确认、忽略，或同时人工修订字段
allday-asr action-review 3 --status confirmed
allday-asr action-review 3 --status confirmed `
  --title "与老师见面" `
  --scheduled-at "2026-08-29T10:00:00+08:00" `
  --location "学校"
allday-asr action-review 3 --status dismissed
```

确认操作目前只更新本地候选状态，**不会写入任何真实日历或待办应用**。

## 结果与隐私

- 私人数据：`data/`、`state/`、`outputs/`。
- 模型缓存：`models/`。
- SQLite、原音、转写、试听片段和声纹均不提交 Git。
- 原始录音是永久保存的不可变证据，只能读取；所有派生音频写入独立缓存或 `outputs/`。
- 当前 V1 已有的整段标准化 WAV 继续保留；V2-A 已支持按逻辑窗口临时解码，不再新增长期整段 PCM。
- 任意片段可用 `allday-asr clip <segment-id>` 导出 WAV 回听。

在持续录制他人前，应遵守当地法律，并在适当场景中完成告知和同意。

## 当前边界

V2-A/V2-B/V2-C 的不可变源对象、schema v6、输入指纹、连续真值、多 run benchmark、Qwen/Fun 双假设、强制对齐和逐 token 源追溯已经实现；V2-C.1 已加入盲选连续任务、Oracle ASR 同边界快照、原始/ITN 双 CER 和 paired bootstrap。生产默认暂时仍运行 V1 SenseVoice，因为独立盲标尚未完成，而不是因为旧 `30.03% vs 14.36%` 已证明 Qwen 普遍更差。V2-D 的重叠说话人时间轴尚未实施。Watch 五分钟分块同步、云端 LLM、桌面确认弹窗和真实日历写入也尚未实现。
