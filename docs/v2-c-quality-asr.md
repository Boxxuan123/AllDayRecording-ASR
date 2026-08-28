# V2-C：质量优先双 ASR 与强制对齐

> 实现日期：2026-08-28
> 默认质量目标：16 GB VRAM；8 GB 以相同 BF16 模型、顺序 batch=1 兼容运行
> 输入不变量：只读原始 Watch 音频，所有 PCM 窗口均为退出即删除的派生缓存

## 1. 固定模型与选择策略

V2-C 不用小模型替换质量模型，也不通过量化换显存。固定的首轮组合为：

| 角色 | 模型 | 保存结果 |
| --- | --- | --- |
| 主假设 | `Qwen/Qwen3-ASR-1.7B` | 原始文本、语言、模型参数、原始响应哈希 |
| 主假设时间戳 | `Qwen/Qwen3-ForcedAligner-0.6B` | 中文字符/英文词的开始与结束时间 |
| 第二假设 | `FunAudioLLM/Fun-ASR-Nano-2512` | 独立文本及模型自身 CTC 时间戳 |

主模型结果不会覆盖第二模型，第二模型也不会自动改写主模型。两者规范化后的字符编辑距离形成分歧队列；当前只按差异程度排序，不假装自动裁决哪个模型正确。以后关键实体、否定词、数字等语义分歧可以在同一队列上增加优先级规则。

官方参考：

- [Qwen3-ASR 官方仓库](https://github.com/QwenLM/Qwen3-ASR)
- [Qwen3-ASR-1.7B 模型卡](https://huggingface.co/Qwen/Qwen3-ASR-1.7B)
- [FunASR 官方使用说明](https://github.com/modelscope/FunASR/blob/main/docs/tutorial/README.md)
- [Fun-ASR-Nano-2512 模型卡](https://modelscope.cn/models/FunAudioLLM/Fun-ASR-Nano-2512)

## 2. schema v6 与不可变证据

schema v6 只追加四组实体：

- `asr_hypotheses`：一个 run、窗口和模型角色对应一条不可变假设。
- `asr_alignment_tokens`：每个字符/词的 analysis/session 双时间坐标，以及是否属于窗口 core。
- `asr_token_sources`：token 对一个或多个不可变 `source_object` 的 SHA-256 和源文件时间范围。
- `asr_disagreements`：同一窗口两份假设的规范化距离、优先级和原始文本。

成功写入的 hypothesis、token、source reference 和 disagreement 都受到 SQLite 触发器保护，不能更新或删除。模型或配置变化必须创建新 `processing_run`。每个 token 的源引用在事务内检查：SHA-256 必须一致，源范围必须有效、连续且恰好覆盖 token 的 session 时间范围。

## 3. 五分钟窗口与边界去重

当前长 M4A 和未来 Watch 五分钟块统一投影为 session 时间轴，再生成默认 300 秒 core、前后各 5 秒 analysis context 的逻辑窗口。五分钟是上传、调度和提交单位，不是强迫模型一次处理五分钟连续波形：Qwen 路径先用 CPU FSMN-VAD 找出最长 30 秒的语音区间，合并不超过 600 ms 的短间隔，并在两侧各保留 750 ms 声学上下文；相邻 padding 相撞时以中点切开，避免重复 token。这个做法既跳过大量静音，又避免 Qwen ForcedAligner 在官方 180 秒上限附近出现对齐覆盖下降。

数据库保留 analysis 范围内全部 token；只有“时间中点落在五分钟 core 内”的 token 会进入冻结 benchmark 视图。这样保留跨上传块的边界诊断证据，又不会把上下文文字重复提交两次。主/次假设的规范化文本与 token 串还会计算 alignment coverage；低于 85% 的窗口自动提升为高优先级复核。

逻辑窗口临时解码为 16 kHz mono PCM，模型完成后立即删除。原始 M4A 不移动、不重命名、不转码、不写元数据。

## 4. 运行配置

默认 `allday-asr.toml` 明确选择 `quality-16gb`：

- BF16，无量化。
- Qwen batch=4。
- 300 秒 core + 5 秒上传边界 context；内部为 30 秒以内的 VAD utterance + 750 ms 声学 context。
- `max_new_tokens=4096`。

当前 8 GB 5070 使用 `compatible-8gb`：模型和精度完全相同，只把 Qwen batch 降为 1。管线先完成全部 Qwen+Aligner 窗口并释放 CUDA，再加载 Fun-ASR-Nano，避免两套 ASR 同时常驻。

```powershell
# 8 GB 机器先做一个真实五分钟 smoke test
allday-asr asr-v2 run 1 --profile compatible-8gb --max-windows 1

# 8 GB 全量；16 GB 机器省略 --profile 即使用默认质量档
allday-asr asr-v2 run 1 --profile compatible-8gb
allday-asr asr-v2 run 1

# 查看逐窗口证据和分歧
allday-asr asr-v2 status <run-id>
```

如果中途异常，已成功写入的窗口不会丢失。使用完全相同的配置续跑，程序只补缺少的 `(window, role)`：

```powershell
allday-asr asr-v2 run 1 --profile compatible-8gb --resume-run-id <run-id>
```

`--max-windows` 是 smoke-test 范围的一部分，因此带该参数的 run 不能当成全长 run 续跑。

## 5. 冻结并与 V1 比较

完成的 run 可以把主模型 core token 冻结成 V2-B prediction set。`transcript` 与 `alignment_token` 都使用每个 token 的实际时间区间；不能把整份五分钟文字标为一个 transcript 区间，否则稀疏真值只要与窗口相交，就会把窗口内无关文字错误计为插入：

```powershell
allday-asr asr-v2 snapshot <run-id> --name qwen3-asr-1.7b-v2c
allday-asr benchmark run 1 <prediction-set-id>
allday-asr benchmark compare 1
```

当前 truth set 1 只覆盖前 15 分钟且来自 V1 稀疏标注。CER 可与冻结 V1 比较；VAD、DER/JER 和完整对齐指标仍应按真值 completeness 显示 N/A，不能从缺失标注推导质量。

### 5.1 当前长录音验收结果

2026-08-28 在 RTX 5070 Laptop 8 GB 上以 `compatible-8gb` 完成 run 7：

| 项目 | 结果 |
| --- | ---: |
| 输入时长 | 2:44:35.776 |
| 端到端耗时 | 8:14.052 |
| 五分钟窗口 | 33 |
| 主/第二假设 | 33 / 33 |
| 对齐 token | 6,147 |
| 双模型分歧窗口 | 16 |
| 对齐覆盖低于 85% 的非空假设 | 3 |
| Qwen 非空假设平均对齐覆盖率 | 93.05% |
| Fun-ASR 非空假设平均对齐覆盖率 | 100% |
| token 源范围覆盖错误 | 0 |

prediction set 3 使用修正后的 token 时间区间，truth set 1 上 CER 为 `0.3003`；V1 SenseVoice 为 `0.1436`。因此 V2-C 完成了候选模型和证据管线，但 Qwen 不满足“真实 Watch 真值显著优于 V1”的晋级规则，当前不得替换生产基线。主要差异集中在远场短句、电视声遮蔽和数字；后续可利用已保存的两份假设和 16 个分歧窗口做场景路由或最小音频云端复核。

曾生成的 prediction set 2 把整个五分钟窗口作为 transcript 区间，得到 `0.7859`。该结果是快照适配器缺陷，不代表模型质量；由于预测集和 benchmark run 不可变，它作为审计记录保留，新适配器以独立 prediction key 冻结 set 3，没有覆盖旧证据。

## 6. 依赖与模型缓存

Qwen 官方 `qwen-asr==0.0.6` 固定 `transformers==4.57.6`。2026-08 的最新 Gradio 6 要求较新的 `huggingface-hub`，与这条固定依赖冲突，因此项目显式固定兼容的 `gradio==5.49.1`、`huggingface-hub==0.36.2` 和 `tokenizers==0.22.2`。这不改变推理代码，只消除官方包把可选网页演示放入主依赖后产生的解析冲突。

权重存放在被 Git 忽略的 `models/`，运行结果、数据库和私人音频分别位于被忽略的 `outputs/`、`state/`、`data/`。任何模型下载和重装都不应触碰原始录音。
