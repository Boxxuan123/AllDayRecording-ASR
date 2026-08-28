# V2-D：重叠感知说话人时间轴

## 1. 当前结论

V2-D 的工程实现采用本地 `pyannote/speaker-diarization-community-1` 作为默认高质量后端。它同时输出：

- `regular`：允许两名或更多说话人同时存在的原始时间轴；
- `exclusive`：每个时刻只保留一名说话人的 ASR 对齐辅助时间轴；
- V2-C committed token 到匿名说话人的 `primary / overlap / uncertain / none` 归属。

新结果只写入 schema v7 的不可变表，不覆盖 V1 的 `speech_segments.speaker_session_id`，也不修改或重编码 Watch 原始 M4A。模型读取的是由 source/session 图临时派生的 16 kHz 单声道 PCM，退出上下文后删除。

当前代码和存储链路已经通过合成重叠回归测试；真实长录音模型运行仍需先在 Hugging Face 接受 Community-1 条款并向当前进程提供 `HF_TOKEN`。这个 token 不写配置、数据库或运行清单。

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

首个真实 V2-D run 可以先用于发现时间轴、检查重叠和生成后续盲标候选。要形成模型晋级结论，需要另建小而信息密集的说话人真值：在选定用餐对话窗口内穷尽标出现场说话人、电视/媒体声和重叠，并保持同一匿名人物标签一致。电视声可以独立标为 `media_00`，不能静默省略；身份分类仍与匿名 diarization 分开。

## 7. 尚未包含

- Community-1 gated 权重没有随仓库分发。
- Sortformer backend 尚未安装；协议已经可插拔，只有在同一新真值上比较后才决定是否增加。
- `SPEAKER_00` 等只是单次会话内匿名标签，不等于本人、父母或电视；跨天身份与本人验证是下一层。
- 现有网页尚未增加 V2-D 可视化轨道；当前通过数据库、CLI status 和 benchmark snapshot 消费。
