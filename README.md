# AllDayRecording-ASR

一个本地优先的全天录音处理原型：将华为 Watch 导出的长录音离线处理为带时间戳、可回听、可人工校正身份的文字时间线。

当前 **可评测的一键离线日记 V1** 仍可完整运行；V2-A/V2-B/V2-C 已完成不可变原音、逻辑窗口、连续时间真值、多 run benchmark、Qwen3-ASR-1.7B 强制对齐和 Fun-ASR-Nano 第二假设。V2-D 已完成 Community-1 重叠/互斥时间轴和 token-to-speaker；V2-D.1 把确定/可能语音、媒体来源和匿名 speaker 解耦；V2-D.2 又恢复稀疏人工 `mother/father/tv` 真值，按短区间揭示匿名簇污染而不全局改名。V2-E.0.1 已实现“完整对话优先”的不联网语义证据契约：完整对话、provider 传输任务和 120 秒回听片互不混用；真实云端 LLM 尚未接入。五块 V2-C 开发集仍需新的未见 holdout；现有人工 speaker 标注不穷尽电视声，不能直接报告公平 DER/JER。V2 不以实时性或小模型为目标；之后需补充干净父母声纹样本，再接真实云端 provider 和 Watch 同步。

实施依据见 [V2 质量优先架构与实施设计](docs/v2-quality-first-architecture.md)。当前结果、风险和进度见 [项目现状与路线图](docs/project-status.md)，V2-B 操作见 [连续时间真值与 Benchmark 指南](docs/v2-b-continuous-benchmark.md)，V2-C 操作见 [质量优先双 ASR 与强制对齐](docs/v2-c-quality-asr.md)，公平性修订见 [V2-C.1 公平基准重建](docs/v2-c1-fair-benchmark.md) 和 [V2-C.2 声学富集盲测](docs/v2-c2-acoustic-blind-benchmark.md)，门控实现见 [V2-C.3 双 VAD 证据门控](docs/v2-c3-speech-gating.md)，说话人路线和命令见 [V2-D 重叠感知说话人时间轴](docs/v2-d-speaker-timeline.md)，语义接口边界见 [V2-E.0.1 完整对话优先证据层](docs/v2-e0-semantic-evidence.md)。

## 已验证环境

- Windows、Python 3.12。
- RTX 5070 Laptop GPU，8 GB 显存。
- PyTorch / torchaudio `2.9.1+cu128`、torchvision `0.24.1+cu128`。
- FunASR `1.4.4`、ModelScope `1.39.1`、Qwen-ASR `0.0.6`、Silero VAD `6.2.1`、pyannote.audio `4.0.7`。
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

V2-C.3 全量 run 10 也已在 8 GB 上完成：33+33 份假设、312 个门控候选、287 个接受、25 个拒绝、2,192 个 committed 主 token，耗时 460.534 秒且未 OOM。旧 run 7 的全部 6,147 个审计 token 数量不变，新管线通过元数据决定哪些 token 可以提交，不删除旧证据。

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

## V2-D：重叠感知说话人时间轴

schema v7 将说话人时间轴与 V1 VAD segments 解耦，同时保存允许重叠的 regular turns 和便于 ASR token 协调的 exclusive turns。所有 turn 都有原始 source SHA-256/时间引用；V2-C committed token 可以保存主说话人、并发说话人、不确定或无归属，不再假设一个 VAD 段只能有一人。

```powershell
hf auth login  # 首次下载前先在 Community-1 页面接受条款；也可设置 HF_TOKEN
allday-asr diarization-v2 run 1 --asr-run 10
allday-asr diarization-v2 status <run-id>
allday-asr diarization-v2 snapshot <run-id>
allday-asr diarization-v2 source-truth 1 --start 17:00 --end 17:14 --source media_playback --name v2d1-candidate01-media-17m
allday-asr diarization-v2 refine 1 --truth-set 2 --truth-set 3
allday-asr diarization-v2 identity-audit 1 --truth-set 1
allday-asr diarization-v2 mine-identities 1 --truth-set 1 --identity father
allday-asr diarization-v2 sync-identity-references --identity father
```

音频仍只在本地处理，pyannote telemetry 已关闭；HF token 不进入配置或数据库。完整模型选择、融合阈值和评测边界见 [V2-D 指南](docs/v2-d-speaker-timeline.md)。

真实全量 run 11 使用 Community-1 revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`：识别 4 个匿名说话人、989 条 regular turns、951 条 exclusive turns、68 个重叠区间（29.412 秒）。V2-D.1 run 12 冻结 607 段确定语音和 453 段可能语音。V2-D.2 run 13 对 truth set 1 的 100.450 秒稀疏身份真值做污染审计，确认 02 同时含电视 71.72%、父亲 13.09%、母亲 8.84% 和本人 6.35%；父亲真值覆盖率 88.59%，问题主要是错簇而非漏检。

V2-D.3 run 17 复用 Community-1 内置 WeSpeaker ResNet34，把父亲 5.970 秒稀疏真值组成 2 个弱种子 embedding，同时以母亲、本人和电视作负对照；378 个未见短窗中只展示前 12 条。首轮人工审核为 11/12 命中：高对照 7/7、中对照 4/4、探索项 0/1；新排序因此优先高/中对照，并把同一音频区间的人工结论继承到等价新 run。网页审核保存在 schema v8 的独立覆盖层，不修改完成 run、不自动登记身份，也不持久化生物特征 embedding。稀疏真值仍不能支持公平 DER/JER。

## V2-E.0.2：Episode 语义证据层

schema v10 将 V2-C committed token、V2-D 匿名声簇、冻结的现场/电视来源、区间级真实身份和永久原音坐标组织成供应商无关的本地证据底账。启发式长容器只叫 episode，不再假装是完整对话；utterance 是 provider 唯一主文本，scene/claim/action 才是 LLM 输出。匿名声簇永远不是人物身份，120 秒只用于网页回听。当前先以 Codex manual record/replay 验收同一请求/响应契约，项目运行时不调用云端 API。

```powershell
allday-asr semantic-v2 build 1
allday-asr semantic-v2 status 1
allday-asr web
```

run 20 修复了 120 秒硬切，但把 37 分钟声学容器误称为“完整对话”，且尚未把 source/identity 送入语义层。V2-E.0.2 将在同一 2,192 个 committed token 上重建 episode 输入和 Codex 人工语义响应；历史 run 不删除。稳定实体、验证规则、隐私边界和 V2-E.1 进入条件见 [V2-E.0.2 指南](docs/v2-e0-semantic-evidence.md)。

## V2-C.1/V2-C.2：公平基准

```powershell
# 只依赖原音指纹、时长和 seed 生成连续盲标范围；任务不含模型输出
allday-asr benchmark init-blind 1 --name watch-blind-30m-v2c1-20260828 `
  --duration 30:00 --chunk 5:00 --seed v2c1-primary-20260828

# 启动不读取数据库和模型结果的本地盲标网页；默认自动打开浏览器
allday-asr benchmark annotate-blind `
  state\evaluations\session-000001\watch-blind-30m-v2c1-20260828-blind-v2c1\truth-draft.jsonl

# V2-C.2：用原始波形声学活动排序生成 10 个分散的一分钟块
allday-asr benchmark init-blind-v2c2 1 `
  --name watch-speech-enriched-10m-v2c2-20260828 `
  --review-duration 10:00 --chunk 1:00 --minimum-gap 1:00 `
  --seed v2c2-primary-20260828

# 标注实际 V2-C.2 任务
allday-asr benchmark annotate-blind `
  state\evaluations\session-000001\watch-speech-enriched-10m-v2c2-20260828-blind-v2c2\truth-draft.jsonl

# 在同一人工 transcript 边界上比较纯 ASR
allday-asr benchmark oracle-asr 1 --model sensevoice --name oracle-sensevoice
allday-asr benchmark oracle-asr 1 --model qwen --name oracle-qwen
allday-asr benchmark compare-oracle-pair 1 <baseline-set> <candidate-set> `
  --samples 200000 --itn

# 人工无法完成全部窗口时，只冻结评测前已标 complete 的明确预备子集
allday-asr benchmark freeze-completed <truth-draft.jsonl> `
  --name v2c2a-preliminary

# 将全量 Qwen token 裁剪到非连续 review-region 后再做端到端评分
allday-asr asr-v2 snapshot <run-id> --truth-set <truth-set-id> `
  --name qwen-review-scoped
```

V2-C.1 的均匀连续 30 分钟块经人工抽听和客观音量复核后确认接近全静音：以 `-50 dBFS` 为阈值时每个五分钟块只有约 `1%–2%` 非静音代理。它保留为环境负样本，用于 VAD 误报和 ASR 幻觉率，不再承担主 CER 排名。

V2-C.2 对整段不可变 PCM 只计算 100 ms RMS 活动、持续活动、P90/RMS 音量和 200–4000 Hz 能量比例，不读取候选 VAD、ASR 或旧转写。真实任务选择了 `00:11–00:37` 之间 10 个相隔至少一分钟的一分钟块，独立 `-50 dBFS` 检查的非静音代理为 `86.7%–100%`。数据库仍保存包围范围，但 benchmark 只计算十个明确的 review-region，未抽中的间隙不会被误当作人工确认的静音。网页可记录语音起止、准确听写或无法可靠听清，并将声源标为现场、电视/媒体、现场与媒体重叠或不确定；重叠声不要求人工强行分离。旧标注按现场人声兼容，编辑会撤销对应块的完成状态。导入必须验证 selection manifest、窗口/音频 SHA-256、声源值、完整复核和 `model_outputs_unseen` 声明。完整设计见 [V2-C.2 指南](docs/v2-c2-acoustic-blind-benchmark.md)。

当前人工在前五块停止，系统将其冻结为明确的 V2-C.2a 预备子集，没有伪装成完整十分钟 holdout。同一人工边界上，18 段现场人声的 SenseVoice/Qwen/Fun-ASR raw CER 分别为 `49.02%/32.35%/54.41%`；Qwen 相对 SenseVoice 的 20 万次 paired bootstrap 95% 区间为 `[-29.61, -8.12]` 个百分点，确认 Qwen 是当前主识别模型。旧完整流水线五块 CER 为 `109.94%`，V2-C.3 降为 `92.27%`；排除唯一纯电视窗口后从 `154.90%` 降为 `116.18%`，确认提升来自现场人声而不是电视样本。由于同一五块已参与阈值选择，这只是开发证据，下一步必须使用新 holdout。完整结果见 [V2-C.3 指南](docs/v2-c3-speech-gating.md)。

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
- 查看 V2-D 多说话人轨道，并优先试听自动排序的多人对话、重叠说话和成组无归属文字。
- 查看 V2-E.0.1 本地语义证据包，按完整对话切换短回听片、编辑并追加确认或驳回记录。
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

这一步目前只建立人物档案和样本库；**跨天自动识别该人物尚未实现**。V2-D.3 已能把冻结真值和候选人工结论保存为跨录音可复用的永久原音坐标参考集，但不会因累计时长接近门槛就自动生成声纹或绑定身份。日常对话无需要求所有人预先上传声纹，默认保留为会话级匿名人物。

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

V2-A/V2-B/V2-C 的不可变源对象、输入指纹、连续真值、多 run benchmark、Qwen/Fun 双假设、强制对齐和逐 token 源追溯已经实现；V2-C.3 的双 VAD 证据门控、committed token 快照和显式 VAD prediction 已实现。V2-D 的 schema v7、Community-1 backend、重叠/互斥 speaker turns、token-to-speaker 融合、speaker/overlap snapshot 和本地网页轨道已实现并完成全量 run 11；生产默认仍不覆盖 V1。Watch 五分钟分块同步、云端 LLM、穷尽 speaker 真值、跨天身份、桌面确认弹窗和真实日历写入尚未实现。
