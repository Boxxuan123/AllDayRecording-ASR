# AllDayRecording-ASR

一个本地优先的全天录音处理原型：将华为 Watch 导出的长录音离线处理为带时间戳、可回听、可人工校正身份的文字时间线。

V3.0-G 已把 V3 Core、版本化 Desktop API 和独立前端设为默认入口。V3.1-A/B/C 已完成可靠统一对话时间线；V3.2 已完成证据、事件、记忆三层数据架构；V3.3 进一步完成提醒候选校验、去重、冲突处理、人工确认、事件更新/取消/完成、证据回听、Phone 投影与 HarmonyOS 系统定时提醒闭环，并接入本机已登录的 Codex 做用户触发的文本提取。Codex 只接收所选 utterance 和同会话活动提醒，不上传音频或本地路径，在专用空目录中以只读沙箱、拒绝审批方式运行；候选默认仍须审核。V2-A.1 至 V2-E.0.2 的模型、证据和评测能力仍完整保留，但只通过 `allday-asr legacy ...` 作为可执行回退或 V3 adapter 使用；Legacy 网页强制 query-only，拒绝所有写请求。现有本人校准只有 2 个正例，且真实连续真值均为 `alignment=none`，所以旧数据身份保持 `unknown`、真实跨切片发布门禁保持拒绝；系统不会把候选分数或缺失真值冒充成功证据。

## V3.0 默认入口与回退

```powershell
# V3 Desktop 默认入口；仅监听本机并只调用 /api/v3
allday-asr web

# 默认启用 /device/v3 的 Phone 接收、上传和增量同步服务
allday-asr device receive

# V2 页面和命令只在明确的 Legacy 命名空间下提供
allday-asr legacy --help
allday-asr legacy web
```

当前完整 V3.3 的契约、Core schema、Phone projection schema 和默认入口冻结在
`contracts/v3/release-lock.json`；V3.0 的切换证据仍保留在独立验收文档与 Git 历史中。本次真实 V2 数据切换使用 `release-prepare` 创建两份
逐文件 SHA-256 校验、不可覆盖且只读的数据库/音频快照；`release-verify` 可随时复算证据。
生产 Device listener 还必须通过 `ALLDAY_V3_DEPLOYMENT=production`、真实 Passkey RP ID
和可信 origin 的 fail-closed 校验。

真实迁移证据、回退步骤与尚待执行的生产/三端真机签字项见
[V3.0-G 迁移、切换与发布验收](docs/V3/V3.0-G-migration-cutover-release.md)。
V3.1 统一时间线与校正闭环的边界、迁移和自动化证据见
[V3.1-A 统一时间轴与校正闭环](docs/V3/V3.1-A-unified-timeline-corrections.md)。
本人身份三态投影、阈值门禁和校正边界见
[V3.1-B 本人身份投影与阈值证据](docs/V3/V3.1-B-self-identity-projection.md)。
跨切片接缝真值、错位指标和发布门禁见
[V3.1-C 跨切片对齐评测与完成门禁](docs/V3/V3.1-C-cross-chunk-alignment-gate.md)。
三层对象、模型写入边界、来源追溯与重算闭环见
[V3.2 三层知识架构与可重算闭环](docs/V3/V3.2-three-layer-knowledge-architecture.md)。
提醒候选审批、事件生命周期、证据回听、跨端投影和系统定时提醒边界见
[V3.3 智能提醒闭环](docs/V3/V3.3-intelligent-reminder-loop.md)。

实施依据见 [V2 质量优先架构与实施设计](docs/v2-quality-first-architecture.md)。当前结果、风险和进度见 [项目现状与路线图](docs/project-status.md)，新分片正式使用前的堵点和准入顺序见 [新音频正式使用前准入审计](docs/new-audio-production-readiness.md)。V2-B 操作见 [连续时间真值与 Benchmark 指南](docs/v2-b-continuous-benchmark.md)，V2-C 操作见 [质量优先双 ASR 与强制对齐](docs/v2-c-quality-asr.md)，公平性修订见 [V2-C.1 公平基准重建](docs/v2-c1-fair-benchmark.md) 和 [V2-C.2 声学富集盲测](docs/v2-c2-acoustic-blind-benchmark.md)，门控实现见 [V2-C.3 双 VAD 证据门控](docs/v2-c3-speech-gating.md)，说话人路线和命令见 [V2-D 重叠感知说话人时间轴](docs/v2-d-speaker-timeline.md)，语义接口边界见 [V2-E.0.2 Episode 证据层](docs/v2-e0-semantic-evidence.md)。

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
allday-asr legacy doctor
```

## 手机到电脑文件接收

电脑端已经提供独立的局域网接收服务；它不会把仅限本机的网页工作台或数据库接口开放给手机：

```powershell
allday-asr device receive
```

终端会显示配对二维码路径、局域网 HTTPS 地址、配对 CA 指纹和接收目录。手机首次扫码后用 Passkey 授权登记 HUKS 设备公钥；之后电脑通过 mDNS 广播当前地址，手机用稳定接收端标识自动发现，并以设备私钥静默签署一次性挑战。切换 Wi-Fi 不需要改地址或重新认证。协议同时支持按原相对路径保存、逐文件 SHA-256 完整性校验、幂等重试和服务重启后的断点续传；最终文件默认进入 `data\phone-inbox`，同路径的不同内容永不覆盖。接收服务默认拒绝明文 HTTP。

鸿蒙手机上传端已支持扫码配对、自动发现和断点上传。完整接口和安全边界见 [手机到电脑文件传输协议](docs/phone-to-computer-transfer.md)。

需要在整段会话上传完成后自动开始 V2 时，以最后一个
`session_summary.json` 的完整校验作为提交信号。当前没有电脑可验证的独立备份时，
需显式接受 shadow 模式：

```powershell
allday-asr device receive --auto-workflow --workflow-shadow
```

正式 production 自动流程改用
`--workflow-backup-root <独立磁盘或网络目录>`；接收端会先自动导入并完成备份与恢复
演练，再串行运行 V2，手机无需等待模型完成。接收服务重启后也会恢复尚未处理完的
manifest 自动任务。

## V2-A/V2-A.1：不可变原音、分片会话与逻辑窗口

schema v11 在原有 source/session 图上增加 `source_instance` 和不可变 `session_manifest`：SHA-256 相同的静音分片可共享内容对象，但每次真实采集仍有独立文件实例和时间位置。schema v12 再增加逐实例备份清单、当前字节校验和恢复演练证据。清单先完整检查所有文件、采样坐标、格式和哈希，再在单个事务中创建关闭会话；任何一条失败都不会留下半个会话。原始音频不移动、不改名、不重编码，关闭后的清单、会话、映射和备份文件证据均受触发器保护。

```powershell
# 查看永久原始对象和对应会话
allday-asr legacy sources

# 只读校验 SHA-256、字节数、时长、编码、采样率和声道
allday-asr legacy source-audit

# 为会话 1 规划 5 分钟核心窗口和两侧 5 秒上下文；只规划，不落盘 PCM
allday-asr legacy session-windows 1 --window-seconds 300 --context-seconds 5

# 导入采集端清单；重复导入同一清单返回同一个 session_id
allday-asr legacy session import-manifest <session-manifest.json>
allday-asr legacy session list
```

逻辑窗口可以跨越多个未来 Watch 5 分钟块；只有模型真正运行时才临时解码当前窗口，退出后立即删除。当前 V1 已有的整段标准化 WAV 保留用于兼容和复现，但 V2-A 不再创建新的长期整段 PCM。

## V2-B：连续时间真值与多 run Benchmark

schema v5 将真值、预测快照和报告都绑定到同一个不可变输入指纹。旧 `segment_id` 只作为迁移来源说明，不再是评测主键；替换 VAD segments 不会使冻结真值失效。

```powershell
# 无损迁移已有 15 分钟 V1 标注
allday-asr legacy benchmark migrate-v1-truth `
  state\evaluations\recording-000001\baseline-first-15m.jsonl

# 冻结当前 V1 结果、运行并比较 benchmark
allday-asr legacy benchmark snapshot-v1 1 --name v1-sensevoice-current
allday-asr legacy benchmark run 1 1
allday-asr legacy benchmark compare 1

# 为真正穷尽式的 VAD/DER 标注创建新模板
allday-asr legacy benchmark init-truth 1 --name first-15m-exhaustive --end 15:00
```

当前迁移基线包含 277 条连续时间事实和 971 条冻结预测，CER 为 `14.36%`。旧标注不是穷尽式 VAD/说话人真值，因此 VAD-F1、DER/JER 显示 `N/A`，不会把未标时间错误地当成非语音。完整格式与指标定义见 [V2-B 指南](docs/v2-b-continuous-benchmark.md)。

## V2-C：最大模型双假设与逐 token 追溯

schema v6 为每个五分钟窗口不可变保存 Qwen3-ASR-1.7B 主假设、Qwen3-ForcedAligner 时间戳、Fun-ASR-Nano 第二假设和分歧队列。每个 token 同时保存 session 时间与原始 M4A 的 source 时间、对象 ID 和 SHA-256；窗口上下文 token 保留作诊断，冻结视图只取 token 中点位于 core 的内容以避免重复。

当前 8 GB RTX 5070 已完成 2 小时 44 分 35 秒录音的全量 BF16 运行：33 个主假设、33 个第二假设、6,147 个对齐 token 和 16 个分歧窗口，耗时约 8 分 14 秒且未 OOM。全部 token 的源范围覆盖检查为 0 错误。旧 token 时间快照的 `30.03%` 与 V1 的 `14.36%` 混合了切分/对齐差异，并且真值本身由 V1 segment 产生，不能再解释为纯模型排名。V2-C.1 同边界重跑为 SenseVoice `15.93%`、Qwen `20.63%`（ITN `19.84%`）、Fun-ASR `25.85%`（ITN `24.80%`）；这仍是 V1 条件化诊断集，最终晋级等待独立盲测。

V2-C.3 全量 run 10 也已在 8 GB 上完成：33+33 份假设、312 个门控候选、287 个接受、25 个拒绝、2,192 个 committed 主 token，耗时 460.534 秒且未 OOM。旧 run 7 的全部 6,147 个审计 token 数量不变，新管线通过元数据决定哪些 token 可以提交，不删除旧证据。

```powershell
# 默认 auto：按实际显存选择 batch；模型、BF16 精度和证据规则不变
allday-asr legacy asr run 1

# 需要时也可以显式固定档位
allday-asr legacy asr run 1 --profile compatible-8gb
allday-asr legacy asr run 1 --profile quality-16gb

# 冻结主假设并与 truth set 1 / V1 基线比较
allday-asr legacy asr snapshot <run-id> --name qwen3-asr-1.7b-v2c
allday-asr legacy benchmark run 1 <prediction-set-id>
allday-asr legacy benchmark compare 1
```

完整配置、续跑语义、边界策略和依赖固定原因见 [V2-C 指南](docs/v2-c-quality-asr.md)。

## V2-D：重叠感知说话人时间轴

schema v7 将说话人时间轴与 V1 VAD segments 解耦，同时保存允许重叠的 regular turns 和便于 ASR token 协调的 exclusive turns。所有 turn 都有原始 source SHA-256/时间引用；V2-C committed token 可以保存主说话人、并发说话人、不确定或无归属，不再假设一个 VAD 段只能有一人。

```powershell
hf auth login  # 首次下载前先在 Community-1 页面接受条款；也可设置 HF_TOKEN
allday-asr legacy diarization run 1 --asr-run 10
allday-asr legacy diarization status <run-id>
allday-asr legacy diarization snapshot <run-id>
allday-asr legacy diarization source-truth 1 --start 17:00 --end 17:14 --source media_playback --name v2d1-candidate01-media-17m
allday-asr legacy diarization refine 1 --truth-set 2 --truth-set 3
allday-asr legacy diarization identity-audit 1 --truth-set 1
allday-asr legacy diarization mine-identities 1 --truth-set 1 --identity father
allday-asr legacy diarization sync-identity-references --identity father
```

音频仍只在本地处理，pyannote telemetry 已关闭；HF token 不进入配置或数据库。完整模型选择、融合阈值和评测边界见 [V2-D 指南](docs/v2-d-speaker-timeline.md)。

真实全量 run 11 使用 Community-1 revision `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`：识别 4 个匿名说话人、989 条 regular turns、951 条 exclusive turns、68 个重叠区间（29.412 秒）。V2-D.1 run 12 冻结 607 段确定语音和 453 段可能语音。V2-D.2 run 13 对 truth set 1 的 100.450 秒稀疏身份真值做污染审计，确认 02 同时含电视 71.72%、父亲 13.09%、母亲 8.84% 和本人 6.35%；父亲真值覆盖率 88.59%，问题主要是错簇而非漏检。

V2-D.3 run 17 复用 Community-1 内置 WeSpeaker ResNet34，把父亲 5.970 秒稀疏真值组成 2 个弱种子 embedding，同时以母亲、本人和电视作负对照；378 个未见短窗中只展示前 12 条。首轮人工审核为 11/12 命中：高对照 7/7、中对照 4/4、探索项 0/1；新排序因此优先高/中对照，并把同一音频区间的人工结论继承到等价新 run。网页审核保存在 schema v8 的独立覆盖层，不修改完成 run、不自动登记身份，也不持久化生物特征 embedding。稀疏真值仍不能支持公平 DER/JER。

## V2-E.0.2：Episode 语义证据层

schema v10 将 V2-C committed token、V2-D 匿名声簇、冻结的现场/电视来源、区间级真实身份和永久原音坐标组织成供应商无关的本地证据底账。启发式长容器只叫 episode，不再假装是完整对话；utterance 是 provider 唯一主文本，scene/claim/action 才是 LLM 输出。匿名声簇永远不是人物身份，120 秒只用于网页回听。当前先以 Codex manual record/replay 验收同一请求/响应契约，项目运行时不调用云端 API。

```powershell
allday-asr legacy semantic build 1
allday-asr legacy semantic export 1 state\evaluations\session-000001\v2e02-request.json
allday-asr legacy semantic replay 1 state\evaluations\session-000001\v2e02-response.json
allday-asr legacy semantic status 1
allday-asr legacy web
```

run 20 修复了 120 秒硬切，但把 37 分钟声学容器误称为“完整对话”，且尚未把 source/identity 送入语义层。最终 run 22 在同一 2,192 个 committed token 上重建为 2 个 episode，并由当前 Codex 会话保守提取 9 个 scene、1 个有身份区间支撑的 claim、0 个 action 和 7 个 unresolved；历史 run 均未删除。该结果是不可变的手工 LLM 记录/回放，不冒充已接入的固定模型 API。稳定实体、验证规则、隐私边界和 V2-E.1 进入条件见 [V2-E.0.2 指南](docs/v2-e0-semantic-evidence.md)。

## V2-C.1/V2-C.2：公平基准

```powershell
# 只依赖原音指纹、时长和 seed 生成连续盲标范围；任务不含模型输出
allday-asr legacy benchmark init-blind 1 --name watch-blind-30m-v2c1-20260828 `
  --duration 30:00 --chunk 5:00 --seed v2c1-primary-20260828

# 启动不读取数据库和模型结果的本地盲标网页；默认自动打开浏览器
allday-asr legacy benchmark annotate-blind `
  state\evaluations\session-000001\watch-blind-30m-v2c1-20260828-blind-v2c1\truth-draft.jsonl

# V2-C.2：用原始波形声学活动排序生成 10 个分散的一分钟块
allday-asr legacy benchmark init-blind-v2c2 1 `
  --name watch-speech-enriched-10m-v2c2-20260828 `
  --review-duration 10:00 --chunk 1:00 --minimum-gap 1:00 `
  --seed v2c2-primary-20260828

# 标注实际 V2-C.2 任务
allday-asr legacy benchmark annotate-blind `
  state\evaluations\session-000001\watch-speech-enriched-10m-v2c2-20260828-blind-v2c2\truth-draft.jsonl

# 在同一人工 transcript 边界上比较纯 ASR
allday-asr legacy benchmark oracle-asr 1 --model sensevoice --name oracle-sensevoice
allday-asr legacy benchmark oracle-asr 1 --model qwen --name oracle-qwen
allday-asr legacy benchmark compare-oracle-pair 1 <baseline-set> <candidate-set> `
  --samples 200000 --itn

# 人工无法完成全部窗口时，只冻结评测前已标 complete 的明确预备子集
allday-asr legacy benchmark freeze-completed <truth-draft.jsonl> `
  --name v2c2a-preliminary

# 将全量 Qwen token 裁剪到非连续 review-region 后再做端到端评分
allday-asr legacy asr snapshot <run-id> --truth-set <truth-set-id> `
  --name qwen-review-scoped
```

V2-C.1 的均匀连续 30 分钟块经人工抽听和客观音量复核后确认接近全静音：以 `-50 dBFS` 为阈值时每个五分钟块只有约 `1%–2%` 非静音代理。它保留为环境负样本，用于 VAD 误报和 ASR 幻觉率，不再承担主 CER 排名。

V2-C.2 对整段不可变 PCM 只计算 100 ms RMS 活动、持续活动、P90/RMS 音量和 200–4000 Hz 能量比例，不读取候选 VAD、ASR 或旧转写。真实任务选择了 `00:11–00:37` 之间 10 个相隔至少一分钟的一分钟块，独立 `-50 dBFS` 检查的非静音代理为 `86.7%–100%`。数据库仍保存包围范围，但 benchmark 只计算十个明确的 review-region，未抽中的间隙不会被误当作人工确认的静音。网页可记录语音起止、准确听写或无法可靠听清，并将声源标为现场、电视/媒体、现场与媒体重叠或不确定；重叠声不要求人工强行分离。旧标注按现场人声兼容，编辑会撤销对应块的完成状态。导入必须验证 selection manifest、窗口/音频 SHA-256、声源值、完整复核和 `model_outputs_unseen` 声明。完整设计见 [V2-C.2 指南](docs/v2-c2-acoustic-blind-benchmark.md)。

当前人工在前五块停止，系统将其冻结为明确的 V2-C.2a 预备子集，没有伪装成完整十分钟 holdout。同一人工边界上，18 段现场人声的 SenseVoice/Qwen/Fun-ASR raw CER 分别为 `49.02%/32.35%/54.41%`；Qwen 相对 SenseVoice 的 20 万次 paired bootstrap 95% 区间为 `[-29.61, -8.12]` 个百分点，确认 Qwen 是当前主识别模型。旧完整流水线五块 CER 为 `109.94%`，V2-C.3 降为 `92.27%`；排除唯一纯电视窗口后从 `154.90%` 降为 `116.18%`，确认提升来自现场人声而不是电视样本。由于同一五块已参与阈值选择，这只是开发证据，下一步必须使用新 holdout。完整结果见 [V2-C.3 指南](docs/v2-c3-speech-gating.md)。

## V2-W.0/V2-W.1：分片会话质量工作流与生产准入

新录音不需要伪造一条“代表整个会话”的 `recording`。导入清单得到 `session_id` 后，先把每个原始文件实例和原始采集清单复制到独立设备或网络存储，逐文件复算 SHA-256 并做临时恢复演练。`readiness` 只读检查输入、连续性、备份和当前已验证的处理时长，不运行音频模型：

```powershell
# 1. 原子导入后先检查；没有独立备份时应得到 shadow_ready
allday-asr legacy session readiness <session-id>

# 2. 目标应是真正的独立设备或网络位置；命令从不覆盖既有备份
allday-asr legacy session backup <session-id> <backup-root> `
  --storage-kind independent_device

# 3. 当前字节和恢复演练通过后应得到 production_ready
allday-asr legacy session readiness <session-id>

# 4. 默认执行入口强制要求 production_ready
allday-asr legacy workflow run --session <session-id>
allday-asr legacy workflow status <session-id>

# 只有明确接受风险的受监控实验才绕过生产门；仍会阻断损坏/gap/overlap
allday-asr legacy workflow run --session <session-id> --shadow
```

备份目录由会话键与输入指纹稳定寻址；既有目录只校验不覆盖，每个相同内容的真实文件实例仍分别备份。校验失败会撤销旧的恢复资格。阶段状态和子 run ID 持久保存在 SQLite；进程退出后仍可检查。输入指纹、阶段配置和本地模型签名一致时才复用完成阶段，V2-C 的失败 run 可按既有窗口 checkpoint 续跑。

默认 workflow 在 V2-D 后自动生成 V2-D.1 确定/可能语音双层证据；同一会话存在冻结的人工 speaker 真值时，还会自动运行 V2-D.2 污染审计。D.3 不自动认人：只有身份审计提示问题并由人工选择目标身份后才生成候选，所有候选都必须逐条确认。可能语音达到 10% 或 60 秒、无归属 token 达到 2% 或 20 个、身份覆盖低于 80%、发现污染簇或身份碎片化时，workflow 以 `*_needs_review` 完成；匿名时间轴和语义证据仍可使用，不会被审核支线阻塞。没有 committed token 时基础状态是 `semantic_ready_empty`；有文字时是 `semantic_ready`。所有状态都不调用云端 LLM，也不把临时拼接 PCM 当作原音保存。

这条 CLI 流程已经可以用于新的、已关闭的录音会话。独立存储位置属于每次真实会话的外部条件，不由程序猜测；超过 3 小时的会话目前只允许显式 shadow 运行，进入无人值守日级流程前仍需让 V2-D 按连续语音岛处理，并把网页的 V1/V2 操作明确分开。

## V1 兼容：一键离线日记

所有一键参数集中在 [allday-asr.toml](allday-asr.toml)。先校验配置和数据库版本：

```powershell
allday-asr legacy config-show
```

可以直接传入新音频，也可以复用已经入库的 `recording_id`：

```powershell
allday-asr legacy daily-run data\watch_new.m4a
allday-asr legacy daily-run 1
```

`daily-run` 是旧单文件 V1 兼容入口，会安全执行 ingest → process → diarization → 本人候选 → timeline → export，并生成 `daily-run.md/json`。它不是上面的 V2 分片会话工作流。重复运行时：

- 复用已经完成的 ASR，不重复计算。
- 复用已有说话人标签，避免清除人工身份。
- 不覆盖已经存在的候选审核 README。
- 将最终配置、配置 SHA-256、每一步状态和结果路径写入 SQLite。
- 缺少本人声纹或需要试听时返回 `needs_attention`，但仍然生成可用日记。

## 可视化工作台

启动本地网页：

```powershell
allday-asr legacy web
```

命令会自动打开一个带临时令牌的本机地址。网页默认按录音会话展示，新的
manifest 分片音频完成 V2 后会直接出现在会话下拉框和概览中，无需再转换成
旧版单文件 recording。网页目前可以：

- 选择已入库会话，查看 V2-C token、V2-D 匿名说话人、V2-E 语义证据和完整运行记录。
- 查看 V2-D 多说话人轨道，并优先试听自动排序的多人对话、重叠说话和成组无归属文字。
- 查看 V2-E.0.2 episode 与 LLM scene/claim/action，按短回听片审核证据，并追加确认或驳回记录。
- 逐段播放原音，填写准确文字、真实说话人、本人身份与关键事实。
- 直接生成 CER、说话人成对 F1、本人识别和关键事实评测报告。
- 确认或忽略日程/待办候选；不会写入真实日历。
- 重新执行幂等的离线日记流程，并查看运行历史。

服务固定监听 `127.0.0.1`，不会对局域网开放；录音和真值不会上传。完整说明见 [本地网页工作台](docs/web-console.md)。

工作台源码位于 `all_day_recording_front/`，使用 Vue 3、TypeScript 与 Vite。日常运行
`allday-asr legacy web` 不需要 Node：仓库同时保存 Vite 生成到
`src/allday_asr/web_assets/` 的生产产物，Python wheel 也会携带这些文件。只有修改前端时
才需要执行：

```powershell
cd all_day_recording_front
npm test
npm run build
```

构建保持 `/`、`/assets/app.js`、`/assets/styles.css`、认证 cookie 和全部 `/api/*`
协议不变；独立盲标台的 `blind.html/js/css` 也由同一前端工程发布。

Python 侧的 `allday_asr.web` 是兼容入口；实际实现位于
`src/allday_asr/interfaces/web/`，HTTP server、认证/响应、资源路由、presenter、
后台任务和 application use case 可以分别测试与维护。

## 分步离线流程

```powershell
# 1. 导入并去重
allday-asr legacy ingest data\watch_1787564356920.m4a

# 2. 标准化、VAD 和可断点续跑的 ASR
allday-asr legacy process 1 --max-segments 3
allday-asr legacy process 1

# 3. 生成录音内匿名说话人标签；短片段和弱聚类保持 unknown
allday-asr legacy diarize 1

# 4. 首次使用时，从只有本人声音的独立录音建立声纹
allday-asr legacy enroll-self data\self-voice
allday-asr legacy voice-library sync

# 5. 只生成本人候选，不直接写入身份
allday-asr legacy self-candidates 1 --min-segment-seconds 0.8 --threshold 0.45 --top 30

# 6. 试听并修改 outputs\recording-000001\self-candidates\README.md 后导入
allday-asr legacy import-self-review 1 --threshold 0.36

# 7. 将本次人工真值积累到留出集/负样本库，并查看状态
allday-asr legacy voice-library accumulate 1 --split holdout
allday-asr legacy voice-library status

# 8. 生成事件时间线和完整转写
allday-asr legacy timeline 1
allday-asr legacy export 1 --format markdown
allday-asr legacy export 1 --format jsonl
```

默认中文识别；中英混说测试可使用：

```powershell
allday-asr legacy process 1 --language auto --reprocess-asr
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
allday-asr legacy speaker-samples 1 --speaker speaker_01 --per-speaker 20

# 仅在逐段确认整个聚类纯净时才允许整簇标为本人
allday-asr legacy mark-self 1 speaker_03 --confirmed-pure

# 撤销该录音的本人绑定，保留独立声纹档案
allday-asr legacy unmark-self 1

# 只用于旧结果：在建立任何身份绑定前重新应用质量门槛
allday-asr legacy audit-speakers 1
```

经对方知情同意后，可以登记固定人物的独立声纹：

```powershell
allday-asr legacy voice-library enroll-person "妈妈" data\voice-library\mother
```

这一步目前只建立人物档案和样本库；**跨天自动识别该人物尚未实现**。V2-D.3 已能把冻结真值和候选人工结论保存为跨录音可复用的永久原音坐标参考集，但不会因累计时长接近门槛就自动生成声纹或绑定身份。日常对话无需要求所有人预先上传声纹，默认保留为会话级匿名人物。

## 人工真值与评测

从录音的一个连续时间范围生成私有 JSONL 模板：

```powershell
# 前 15 分钟；start/end 也支持 MM:SS 和 HH:MM:SS
allday-asr legacy evaluation init 1 --name baseline-first-15m --start 0 --end 15:00
```

人工填写 `reference_text`、`reference_speaker`、`reference_identity` 和 `key_facts` 后运行：

```powershell
allday-asr legacy evaluation run state\evaluations\recording-000001\baseline-first-15m.jsonl
```

也可以启动 `allday-asr legacy web`，在只读页面逐段试听；V3.0-G 后 Legacy 页面不再保存修改，新增评测数据必须使用明确的 Legacy CLI。

报告包含 CER、说话人成对 F1、本人识别指标和关键事实召回率。真值和报告分别保存在已被 Git 忽略的 `state/` 与 `outputs/`。

## 日程/待办候选

`daily-run` 会对包含明确日期、时间和行动词的语句执行高置信规则提取。默认还要求检测到“本人提出”或随后由本人说出“好的/可以”等确认语。候选始终保留提议、确认、原音区间和匹配规则作为证据。

```powershell
# 查看待确认项
allday-asr legacy actions 1 --status pending

# 确认、忽略，或同时人工修订字段
allday-asr legacy action-review 3 --status confirmed
allday-asr legacy action-review 3 --status confirmed `
  --title "与老师见面" `
  --scheduled-at "2026-08-29T10:00:00+08:00" `
  --location "学校"
allday-asr legacy action-review 3 --status dismissed
```

确认操作目前只更新本地候选状态，**不会写入任何真实日历或待办应用**。

## 结果与隐私

- 私人数据：`data/`、`state/`、`outputs/`。
- 模型缓存：`models/`。
- SQLite、原音、转写、试听片段和声纹均不提交 Git。
- 原始录音是永久保存的不可变证据，只能读取；所有派生音频写入独立缓存或 `outputs/`。
- 当前 V1 已有的整段标准化 WAV 继续保留；V2-A 已支持按逻辑窗口临时解码，不再新增长期整段 PCM。
- 任意片段可用 `allday-asr legacy clip <segment-id>` 导出 WAV 回听。

在持续录制他人前，应遵守当地法律，并在适当场景中完成告知和同意。

## 当前边界

V2-A.1 的 schema v11、原子清单导入、重复静音文件实例、冻结会话和 session-native C/D/E 已实现；V2-W.1 的 schema v12、不可覆盖备份、逐文件复核、恢复演练、准入状态、自动显存档位、阶段复用和持久工作流也已实现。V2-C.3 双 VAD、V2-D Community-1 与 V2-E.0.2 本地语义证据继续作为质量主链。新的短会话在独立备份后可进入 CLI 正式工作流；手机到电脑已具备一次扫码配对、自动发现和幂等断点传输。超过 3 小时的分岛/缺口感知 V2-D、真实云端 LLM、独立新 holdout、跨天身份和真实日历写入仍未完成，因此不能把“单会话 production_ready”扩张成“全天无人值守产品已完成”。
