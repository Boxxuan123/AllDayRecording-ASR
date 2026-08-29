# V2-D：重叠感知说话人时间轴

## 1. 当前结论

V2-D 的工程实现采用本地 `pyannote/speaker-diarization-community-1` 作为默认高质量后端。它同时输出：

- `regular`：允许两名或更多说话人同时存在的原始时间轴；
- `exclusive`：每个时刻只保留一名说话人的 ASR 对齐辅助时间轴；
- V2-C committed token 到匿名说话人的 `primary / overlap / uncertain / none` 归属。

新结果只写入 schema v7 的不可变表，不覆盖 V1 的 `speech_segments.speaker_session_id`，也不修改或重编码 Watch 原始 M4A。模型读取的是由 source/session 图临时派生的 16 kHz 单声道 PCM，退出上下文后删除。

代码和存储链路已经通过合成重叠回归测试，并于 2026-08-29 完成现有 2 小时 44 分 35 秒录音的 Community-1 全量 run 11。Hugging Face 凭据只通过标准本机登录缓存或进程环境读取，不写项目配置、数据库或运行清单。

## 2. 为什么默认选择 Community-1

截至 2026-08-28，Community-1 官方模型卡明确提供改进的说话人数估计/分配、常规重叠时间轴、专供 ASR 时间戳协调的 exclusive 时间轴，以及下载后的完全离线运行。`pyannote.audio 4.0.7` 是当前固定版本。[Community-1 模型卡](https://huggingface.co/pyannote/speaker-diarization-community-1/blob/main/README.md)、[pyannote.audio 4.0 发布说明](https://github.com/pyannote/pyannote-audio/releases)

NVIDIA Streaming Sortformer 4spk-v2 也是先进路线：它直接输出 80 ms 帧级多说话人活动概率，并以 arrival-order speaker cache 维持长音频说话人顺序。但官方限制是最多 4 人、主要使用英语数据训练、噪声/非英语/超长录音可能退化。当前项目是中文、Watch 远场、电视混入且未来人数不保证小于等于 4，因此它不适合未经本项目真值验证就成为默认后端。[NVIDIA 模型卡](https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2)、[NeMo 官方文档](https://docs.nvidia.com/nemo-framework/user-guide/latest/nemotoolkit/asr/speaker_diarization/intro.html)

两者没有可直接替代本项目真值的公平横向数字，因此这里的选择不是宣称“Community-1 在所有数据上必胜”，而是：

| 条件 | Community-1 | Streaming Sortformer 4spk-v2 |
| --- | --- | --- |
| 中文 Watch 远场 | 默认候选，仍需实测 | 官方提示非英语/噪声可能退化 |
| 重叠表达 | regular turns | 多通道帧级概率 |
| ASR token 协调 | 原生 exclusive turns | 需要自行后处理 |
| 人数限制 | 可配置/自动估计 | checkpoint 固定最多 4 人 |
| 长录音 | 本地整段聚类 | 支持数小时但官方提示可能退化 |
| 当前工程依赖 | 已接入 | NeMo 依赖较重，保留 backend 接口后再做公平候选 |

云端 `precision-2` 没有接入。将私人全天录音上传第三方属于新的数据授权范围；在用户明确授权和定义删除/保留策略之前，V2-D 只做本地推理，并显式关闭 pyannote 匿名 telemetry。

## 3. schema v7

| 表 | 作用 | 不可变性 |
| --- | --- | --- |
| `diarization_turns` | 保存 regular/exclusive 匿名说话人区间、模型元数据和内容哈希 | 禁止 UPDATE/DELETE |
| `diarization_turn_sources` | 将每个 turn 精确映射回原始对象 SHA-256 和 source 时间 | 禁止 UPDATE/DELETE |
| `token_speaker_attributions` | 将指定 V2-C 主模型 core token 关联到一个或多个说话人 | 禁止 UPDATE/DELETE |

每次 V2-D run 还保存完整 source input fingerprint、模型清单、配置哈希、父 V2-C run 和 JSON manifest。失败 run 不会伪装成完成；如果 gated 模型在加载阶段就没有权限，系统不会创建误导性的空 run。

## 4. token-to-speaker 策略

融合不把整条 VAD segment 粗暴标成一人：

1. 用 exclusive turns 与 token 的时间交集选主说话人；默认至少覆盖 token 的 `50%`，且第一名领先第二名至少 `15%`。
2. 只把 regular turns 中真正同时活动的区间当成重叠证据；两个说话人前后相接不算 overlap。
3. 并发说话人默认至少覆盖 token 的 `30%` 才保存 `overlap` 归属。
4. 有交集但证据不足时保存 `uncertain`，完全没有交集保存 `none`；不强造标签。

一枚 token 可以有一条 `primary` 和若干条 `overlap` 记录。每条决定保留各说话人交集毫秒数、比例、阈值和内容哈希，便于以后更换融合策略时建立新 run 对比。

## 5. 运行

第一次使用需在 [Community-1 页面](https://huggingface.co/pyannote/speaker-diarization-community-1) 接受条款并创建只读 token。推荐通过 Hugging Face 官方本机凭据缓存登录；也可以仅在当前 PowerShell 会话提供环境变量：

```powershell
# 推荐：token 进入 Hugging Face 的本机凭据缓存，不进入项目
hf auth login

# 或者只在当前 PowerShell 会话设置
$env:HF_TOKEN = "hf_..."

# 默认选择录音 1 最新的已完成 V2-C run；当前全量候选也可显式写 --asr-run 10
allday-asr diarization-v2 run 1 --asr-run 10

# 只有确实知道人数时才设置，未知时让模型估计
allday-asr diarization-v2 run 1 --asr-run 10 --num-speakers 3

allday-asr diarization-v2 status <run-id>
allday-asr diarization-v2 snapshot <run-id>
```

也可把完整 snapshot 放到项目私有模型目录并使用 `--model-path`，此后可断网运行。Windows 当前 TorchCodec 动态库不可用，backend 按 pyannote 官方支持的方式传入预加载 float32 波形，不依赖 TorchCodec 解码。

## 6. 公平评测边界

目前 V2-C.2 的人工标注刻意只标现场人声，电视节目声音没有完整标注，而且只完成五个短窗口。它不满足“所有说话人活动穷尽 + 跨区间匿名身份一致”的 DER/JER 真值条件，因此 V2-D 不能从这几条标注给出可信 DER/JER，也不能把未标电视声音一律算假阳性。

run 11 可以用于发现时间轴、检查重叠和生成后续盲标候选。要形成模型晋级结论，需要另建小而信息密集的说话人真值：在选定用餐对话窗口内穷尽标出现场说话人、电视/媒体声和重叠，并保持同一匿名人物标签一致。电视声可以独立标为 `media_00`，不能静默省略；身份分类仍与匿名 diarization 分开。

## 7. 首次真实全量结果

| 项目 | run 11 |
| --- | ---: |
| 模型 revision | `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee` |
| 处理时间 | 291.792 秒，RTF 约 0.0295 |
| 观察显存 | 约 2.5 GiB / 8 GiB |
| 匿名说话人 | 4 |
| regular / exclusive turns | 989 / 951 |
| exclusive 语音并集 | 1,056.899 秒，约占全录音 10.70% |
| 重叠 | 68 个区间、29.412 秒 |
| token 主归属 / 不确定 / 无归属 | 1,548 / 54 / 590 |
| 含额外重叠说话人的 token | 116；共 118 条 secondary overlap 归属 |
| 冻结预测 | prediction set 15；989 speaker + 68 overlap predictions |

1,940 条 regular/exclusive turns 全部具有原始 source 引用，源覆盖长度错误为 0；2,192 个 V2-C committed token 全部有至少一条决定。临时整段 WAV 已删除，79,826,205 字节原始 M4A 的 SHA-256 仍为 `3503e63fc61b0b97a91d0ecb1ea925fbbd81ae021790134f538b04c7ccfd2d08`。

## 8. 本地说话人时间轴网页

`allday-asr web` 现在默认打开 V2-D 只读时间轴。网页不会要求先把整段录音人工标完，而是建立三个试听队列：

- “多人对话”以 90 秒窗口、30 秒步长扫描全录音，综合语音覆盖率、ASR token 数、匿名说话人数、说话人切换和重叠时长，选择 12 个互相不过度重复的高信息窗口。
- “重叠说话”保留 Community-1 regular timeline 中每一段真实并发说话，并在前后各补 4 秒试听上下文。
- “无归属文字”把相隔不超过 2 秒的 `none` token 合并，单组最长 45 秒，再补前后上下文，避免逐词人工检查。

run 11 的真实结果被整理成 12 个多人对话候选、68 个重叠候选和 72 组无归属候选（覆盖全部 590 个无归属 token）。最高信息窗口位于 `00:17:00–00:18:30`，包含 4 个匿名说话人、28 次切换和约 51 秒 exclusive 语音；前 12 个候选集中在约 `00:11:00–00:36:00`，与“吃饭期间存在较长对话”的人工记忆相符。该排序只是节省试听时间的导航，不是说话人准确率真值。

每个窗口展示多说话人轨道、重叠斜纹和逐 token 归属。点击 token 可以跳到相应音频位置。试听 WAV 从 V2-A 永久原音的 source graph 按需生成，做响度归一化后写入独立缓存，单次不超过 120 秒；原始 M4A 不会被修改、替换或删除。

## 9. V2-D.1：证据层与来源层解耦

V2-D.1 不尝试把“同属电视节目”的声音强行合并成一个人。电视男声、电视女声和不同节目人物本来就可能是不同人，`SPEAKER_00` 等匿名聚类因此完全沿用 V2-D run 11。新增的是两条正交轨道：

- 语音证据：`detected` 是 Community-1 regular turns 与 V2-C.3 已接受语音的并集；`possible` 是被门控拒绝但仍有对齐 ASR 文字的证据，以及密集语音中不超过 4 秒的短缺口。`possible` 只进入琥珀色试听队列，不生成或改写 speaker 标签。
- 声音来源：人工可独立标记 `media_playback / live_person / mixed_live_media / unknown`。来源真值不等于人物身份，也不能作为合并匿名 speaker 的依据。

```powershell
# 冻结用户确认的来源事实；不提供 speaker 身份
allday-asr diarization-v2 source-truth 1 --start 17:00 --end 17:14 `
  --source media_playback --name v2d1-candidate01-media-17m

# 从最新 V2-D 生成确定/可能双层预测，并在指定真值上同时评测
allday-asr diarization-v2 refine 1 --truth-set 2 --truth-set 3
```

真实 run 12 生成 607 段 `detected`（1,548.125 秒）和 453 段 `possible`（486.932 秒），prediction set 16/17 已冻结。候选 01 的 `17:00–17:14` 来源微型真值为 truth set 3：正式 detected 覆盖 7.111 秒、recall `50.79%`；加上 possible 后覆盖 14 秒、recall `100%`。该微型真值没有负例时间，所以 false alarm 不可定义。

在 truth set 2 的五个开发窗口上，detected/recall-rescue 的 VAD recall 为 `81.28%/98.97%`，false alarm 为 `61.25%/90.22%`。这说明补救层确实找回大量疑似语音，但也非常激进；加上 truth set 2 对电视声并不穷尽，它不能成为正式 speech/speaker 输出，只适合作为人工“可能漏检”队列。网页因此同时展示确定/可能证据、来源真值和原匿名 speaker 三层，避免把证据、媒体来源与人物身份混为一谈。

run 12 完成后再次执行 source audit，79,826,205 字节原始 M4A 状态仍为 `verified`。

## 10. V2-D.2：人工身份真值与匿名簇污染审计

V2-D.2 恢复 truth set 1 中已有的稀疏 `mother / father / tv / self / unknown` speaker 真值，但不把它们当作模型预测，也不把整个 `SPEAKER_XX` 改名。它使用 V2-D exclusive turns 计算两向审计：每个人工身份被拆到哪些匿名 speaker，以及每个匿名 speaker 混入了哪些人工身份。网页把人工身份作为第四条短区间轨道，并在有真值覆盖的 token 上显示“母亲/父亲/电视”角标。

```powershell
allday-asr diarization-v2 identity-audit 1 --truth-set 1
```

真实 run 13 审计了 72 条稀疏身份区间，共 100.450 秒，模型 exclusive turn 覆盖 67.58 秒（67.27%）。主要结果：

| 人工身份 | 真值时长 | 模型覆盖 | 主要匿名 speaker |
| --- | ---: | ---: | --- |
| 电视 | 52.800 秒 | 54.88% | 02=19.100s；03=3.576s；01=3.190s；00=3.109s |
| 母亲 | 39.040 秒 | 80.99% | 03=29.266s；02=2.354s |
| 父亲 | 5.970 秒 | 88.59% | 02=3.487s；01=1.768s；00=0.034s |
| 本人 | 2.070 秒 | 81.69% | 02=1.691s |

这证明“父亲几乎没识别出来”主要不是 VAD 漏检：父亲真值有 88.59% 被 speaker turns 覆盖，但被分进电视占主导的 02 和 01。02 的已审计组成是电视 71.72%、父亲 13.09%、母亲 8.84%、本人 6.35%；它不能绑定任何单一身份。候选 07（11:00–12:30）现在会显示 22.330 秒人工身份真值，并在“35 一斤”等 token 上同时保留 `SPEAKER_02` 模型归属和“父亲”人工角标。

当前父亲只有 5.97 秒稀疏真值，候选 07 内更只有约 2.74 秒清晰片段，而且与电视混叠；这不满足现有声纹登记至少 30 秒、6 个一致 embedding 的质量门槛。V2-D.2 因此只暴露采样缺口，不用污染音频强造父亲/母亲声纹。下一步必须采集或筛出足够的干净父母语音，再比较短 turn 声纹和目标说话人提取。

## 11. 尚未包含

- Community-1 gated 权重没有随仓库分发。
- Sortformer backend 尚未安装；协议已经可插拔，只有在同一新真值上比较后才决定是否增加。
- `SPEAKER_00` 等只是单次会话内匿名标签，不等于本人、父母或电视；跨天身份与本人验证是下一层。
- V2-D.2 的人工角标只覆盖已有稀疏真值；未标区间仍不能自动对应本人、家人或电视。
