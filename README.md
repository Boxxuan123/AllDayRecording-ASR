# AllDayRecording-ASR

一个本地优先的全天录音处理原型：将华为 Watch 导出的长录音离线处理为带时间戳、可回听、可人工校正身份的文字时间线。

当前处于 **V1 离线原型验证阶段**，不是实时录音产品。现有测试录音已经跑通入库、VAD、ASR、匿名说话人分离、本人声纹候选、人工标注、样本库积累和时间线导出。具体结果、风险和下一步见 [项目现状与路线图](docs/project-status.md)，原始范围见 [第一版 MVP 计划](docs/v1-mvp-plan.md)。

## 已验证环境

- Windows、Python 3.12。
- RTX 5070 Laptop GPU，8 GB 显存。
- PyTorch / torchaudio `2.9.1+cu128`、torchvision `0.24.1+cu128`。
- FunASR `1.4.4`、ModelScope `1.39.1`。
- FFmpeg / FFprobe 可用。

PyTorch 使用 CUDA 专用 wheel，应根据显卡和 CUDA 环境单独安装，因此不由 `pyproject.toml` 自动解析。

```powershell
# 已进入 .venv 后，安装项目本身而不重新解析 Torch
python -m pip install -e . --no-deps

# 检查 Python、FFmpeg、模型依赖和真实 CUDA 运算
allday-asr doctor
```

## 推荐离线流程

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

## 结果与隐私

- 私人数据：`data/`、`state/`、`outputs/`。
- 模型缓存：`models/`。
- SQLite、原音、转写、试听片段和声纹均不提交 Git。
- 原始录音只读，派生音频写入 `outputs/`。
- 任意片段可用 `allday-asr clip <segment-id>` 导出 WAV 回听。

在持续录制他人前，应遵守当地法律，并在适当场景中完成告知和同意。

## 当前边界

尚未实现实时音频传输、流式字幕、日程/待办候选、桌面确认弹窗、日历写入和完整 UI。当前说话人分离在电视、远场、重叠讲话及很短语音上仍不可靠，不能把匿名聚类直接当作人物身份。
