# AllDayRecording-ASR 项目现状与路线图

> 盘点日期：2026-08-30
> 当前阶段：可维护性重构阶段 0 已建立测试、CLI、Web、workflow、Ruff 与依赖审查基线；尚未移动生产代码。V2-A.1 多分片准入与 V2-W.1 备份/准入/持久工作流已完成代码和合成数据回归；最近一轮没有读取或分析 `data` 中的新增音频。V2-D Community-1、V2-E.0.2 四轨 Episode 证据和旧录音验收结果继续保留；真实云端 LLM 尚未接入。

## 1. 结论

项目已经越过“技术路线设计”，具备一条可实际运行的离线处理链路。它可以把一段 Watch 长录音变成可追溯的转写和事件时间线，也已经建立了“独立本人声纹 → 候选片段 → 人工真值 → 留出/负样本库”的闭环。

V1 证明了工程闭环，但其“VAD 段即转写段和单一说话人”的数据模型无法可靠表达片段内换人、重叠讲话和多模型假设。V2-A/V2-B 已完成不可变 source/session、跨源逻辑窗口、连续时间真值、不可变预测快照和多 run benchmark；V2-C 已接入 Qwen3-ASR-1.7B、强制对齐和 Fun-ASR-Nano 第二假设。V2-C.1 发现旧真值全部沿用 V1 segment 边界且标注时可见 V1 hypothesis；V2-C.2 又修正“单个均匀连续盲块可能近乎全静音”的信息量问题。V2-C.3 进一步确认 run 7 已有 FSMN-VAD，真正瓶颈是单路 VAD 误报和 padding token 被提交；现已用 FSMN proposal、Silero/相对 SNR/时长证据和强制对齐后的 core commit 修复。五块开发集的现场人声 CER 从 `154.90%` 降到 `116.18%`，但阈值尚未通过新 holdout 验证。

V2-E.0.2 已把 committed token、匿名声纹、不确定性、现场/媒体来源、区间身份和原音坐标冻结成供应商无关的本地底账，并把 episode、provider 传输任务、LLM scene/claim/action 和 120 秒回听片分层。run 22 由当前 Codex 会话在脱敏 provider payload 上完成一次人工 LLM record/replay：没有项目运行时网络调用，没有音频上传，也不冒充可复现的固定模型 API。

新的硬约束是：每个 Watch 原始文件永久保存且永不修改；五分钟或更短分片通过同一虚拟时间轴进入核心管线；流程代码不得针对某一批新音频写特例；本地不运行通用 LLM，语义阶段之后使用可替换云端接口。实时 ASR 不再是既定里程碑。

## 2. 已实现能力

| 模块 | 状态 | 当前能力 |
| --- | --- | --- |
| 环境自检 | 已完成 | 检查 Python、FFmpeg、依赖和真实 CUDA 运算 |
| 录音入库 | V2-A.1 已完成 | schema v11；内容对象/真实文件实例分离、原子幂等清单导入、整数采样坐标、关闭会话冻结、逐实例完整性状态 |
| 原音备份准入 | V2-W.1 已完成代码 | schema v12；独立/网络/同机测试存储类型、逐实例和原清单覆盖、不可覆盖原子复制、当前哈希复核、恢复演练、失败撤销资格、三态 readiness |
| 音频处理 | V2-A 基础完成 | V1 标准化/VAD/断点续跑；V2 逻辑窗口临时解码且不新增长期整段 PCM |
| ASR | V2-C.3 已实现 | Qwen3-ASR-1.7B、0.6B ForcedAligner、Fun-ASR-Nano、FSMN+Silero 证据门控、padding/core 分离、逐 token 原音追溯；等待新 holdout |
| 说话人时间轴 | V2-D.2 已完成 | 在 D.1 证据/来源分层上增加稀疏人工身份轨、簇污染矩阵和 token 人工角标；审计不改写匿名 speaker；等待干净父母声纹与穷尽真值 |
| 匿名说话人 | 原型完成 | CAM++ 聚类、短片段/弱聚类质量门槛、允许 `unknown` |
| 本人身份 | 原型完成 | 独立多录音登记、片段候选、人工导入、阈值校准 |
| 人物样本库 | 已完成 V1 | `accepted`、`holdout`、`negative`、会话隔离、清单 |
| 固定人物登记 | 基础完成 | 经同意可建立 `known_person` 档案；V2-D.3 人工参考区间可跨录音积累，尚无跨天自动匹配 |
| 时间线 | 原型完成 | 规则聚合事件、Markdown/JSON、原音区间追溯 |
| 语义证据 | V2-E.0.2 已完成 | episode/utterance/scene 分层、ASR/匿名声纹/source/identity 四轨证据、最小化 provider payload、严格响应校验、Codex manual record/replay、追加式审核和分片回听；真实云端 provider 待接入 |
| 数据库迁移 | V2-W.1 已完成 | 最新 schema v12；`processing_runs.recording_id` 对 session-native V2 可空，所有历史证据保留，升级前自动备份 |
| 统一配置 | 已完成 V1 | TOML 校验、配置哈希、每次运行完整快照 |
| 一键日处理 | V1/V2 分离 | V1 保留 `daily-run`；V2 默认要求 `production_ready` 后持久编排 C→D→E，本地停在 `semantic_ready`；`--shadow` 是显式实验模式 |
| 人工评测 | V2-B 已完成 | 连续 session/source 真值、预测快照、CER、VAD、DER/JER、对齐、实体和多 run 对比；保留 V1 兼容入口 |
| 行动建议 | 已完成规则 V1 | 明确日期/时间/行动、本人承诺、证据链、确认/忽略；不写真实日历 |
| 本地网页 | V2-E.0.2 已扩展 | 逐段试听标注、说话人时间轴、episode 容器与 scene/claim/action 分层审核、短片回听、评测、一键运行和历史；只监听回环地址 |
| 实时能力 | 未开始 | 尚无流式识别、弹窗或日历写入 |

## 3. 当前真实测试结果

测试对象为一段华为 Watch 录音：2 小时 44 分 35 秒，16 kHz 单声道 AAC/M4A。

| 指标 | 结果 |
| --- | ---: |
| VAD 语音片段 | 423 |
| 检出语音总时长 | 737.53 秒 |
| 完成转写片段 | 423 / 423 |
| SenseVoiceSmall 纯 ASR 时间 | 24.92 秒 |
| 纯 ASR RTF | 0.0338 |
| ASR 峰值显存 | 约 979 MiB |
| 质量审计后保留匿名标签 | 115 片段 |
| 质量审计后 `unknown` | 308 片段 |
| 人工确认本人片段 | 5 |
| 人工身份/负样本标注 | 17 |
| 规则聚合事件 | 10 |
| 前 15 分钟评测模板 | 93 段；87 段纳入 |
| 可听清文字真值 | 46 段、383 字 |
| 清晰片段 CER | 14.36% |
| V2-C Qwen token 时间快照 CER | 30.03%（混合边界误差，不再用于纯模型排名） |
| V2-C.1 同边界 SenseVoice CER | 15.93%（旧 V1 条件化诊断集） |
| V2-C.1 同边界 Qwen CER | 20.63%；ITN 等价 19.84% |
| V2-C.1 同边界 Fun-ASR CER | 25.85%；ITN 等价 24.80% |
| V2-C.1 环境负样本 | 30 分钟，01:41:42–02:11:42；每块约 1%–2% 非静音代理，pending |
| V2-C.2 语音富集盲标 | 10 个分散一分钟块，00:11–00:37；前 5 块为 truth set 2：18 条现场人声、1 条纯电视，后 5 块 pending |
| V2-C.2a 现场 Oracle CER | SenseVoice 49.02%；Qwen **32.35%**；Fun-ASR 54.41% |
| V2-C.2a 现场 Qwen−Sense | -16.67 个百分点；paired bootstrap 95% CI `[-29.61, -8.12]` |
| V2-C.2a 端到端 CER | 全部：V1 117.68%、Qwen 109.94%；仅现场窗口：V1 148.53%、Qwen 154.90% |
| V2-C.3 开发集 CER | 全部 **92.27%**；仅现场 **116.18%**；纯电视 61.39% |
| V2-C.3 开发集 VAD | F1 **61.42%**；miss 27.66%；false alarm 49.66% |
| V2-C.3 五窗口 run 9 | 161 个候选、147 个接受、14 个拒绝、1,301 个 committed 主模型 token |
| V2-C.3 全量 run 10 | 33+33 假设、312 个候选、287 个接受、2,192 个 committed 主 token、460.534 秒、8 GB 无 OOM |
| V2-C 全量运行 | 33+33 假设、6,147 token、16 个分歧、约 8 分 14 秒 |
| V2-D 全量 run 11 | 4 人、989/951 regular/exclusive turns、68 段重叠共 29.412 秒、291.792 秒、约 2.5 GiB 显存 |
| V2-D token 归属 | 2,192 个全部有决定：1,548 primary、54 uncertain、590 none；116 个 token 含重叠说话人 |
| V2-D.1 run 12 | 607 段确定语音 1,548.125 秒；453 段可能语音 486.932 秒；prediction set 16/17 |
| 候选 01 前 14 秒 | truth set 3 = `media_playback`；detected recall 50.79%，recall-rescue 100%（无负例，FA 不可定义） |
| V2-D.2 run 13 | 72 条身份区间、100.450 秒；模型覆盖 67.27%；02=电视/父亲/母亲/本人污染簇 |
| 父亲稀疏真值 | 5.970 秒，覆盖 88.59%；02=3.487s、01=1.768s，主要是错簇而非 VAD 漏检 |
| V2-D.3 run 17 | Community-1 WeSpeaker；父亲 2 个弱种子；378 个未见短窗中展示 12 个 |
| V2-D.3 负对照 | 母亲 39.040s、本人 2.070s、电视 52.800s；候选不自动写身份 |
| V2-D.3 首轮人工审核 | 11/12 命中；高对照 7/7、中对照 4/4、探索 0/1；确认候选 23.340s |
| V2-E.0.1 run 20 | V2-C run 10 + V2-D run 11；2,192 token、2 个完整对话、7 个本地低信息块、1 个 provider job、3 个候选 |
| V2-E.0.1 主对话 | 00:00:53.280–00:38:12.430；37 分 19.150 秒、288 utterance、19 个回听片但只有 1 个语义实体 |
| V2-E.0.1 隐私边界 | `local_mock`；网络/音频字节/源路径/源哈希/数据库 token ID 均不进入 provider；facts/actions=0/0 |
| V2-E.0.2 run 22 | 同一 V2-C run 10 + V2-D run 11；2 个 episode、292 个主 utterance、9 个双 ASR 备选窗口、1 个 provider job |
| V2-E.0.2 Codex manual eval | 9 scene、1 个区间身份支撑的 claim、0 action、7 unresolved；11 个不可变候选 |
| V2-E.0.2 四轨覆盖 | 第一 episode：source `live=48/media=39/unknown=201`；identity `father=13/mother=18/self=2/unknown=255` |
| V2-E.0.2 隐私边界 | `codex_manual_eval` record/replay；项目运行时不联网、不含音频/路径/原音哈希/数据库 token ID；模型标记为 `codex-session-unversioned` |
| 说话人成对 F1 | 0.543（电视标签语义仍需拆分） |
| 本人识别真值 | 2 段本人、70 段非本人；样本不足以判断泛化 |

质量审计后匿名标签分布为：`speaker_00=31`、`speaker_02=8`、`speaker_03=39`、`speaker_04=37`，其余 308 段为 `unknown`。

这组数据证明长录音处理速度和可续跑链路可行，但不能证明说话人识别已经准确。人工试听已确认：原匿名聚类会混入本人、父母和电视，不能整簇绑定真实身份。

## 4. 当前声纹库

本人档案当前包含 23 个活动 embedding、约 173.33 秒有效登记语音，状态为 `active`。样本库有：

- `accepted`：9 条独立登记来源记录。
- `holdout`：5 条来自本次录音的人工确认本人片段。
- `negative`：12 条，包括母亲、电视、混合和不确定样本。
- 当前实验阈值：`0.36`。

这些数据足以验证完整闭环，不足以证明阈值已经泛化。阈值必须在更多日期、地点、设备距离和噪声条件下评测，不能把当前数字当成固定产品参数。

## 5. 已知技术债和风险

1. 首个 15 分钟标注已迁移为连续时间事实，但 46/46 文字边界都来自 V1 segment，模板可见 V1 hypothesis，且不是穷尽式 VAD/说话人真值；它只允许作 V1 条件化诊断。V2-C.1 均匀连续盲块又偶然落在近静音场景，只用于环境误报轨道。V2-C.2a 的五个已完成窗口足以把 Qwen 选为下一版主模型，但属于人工受限的预备开发证据；V2-C.3 调参后必须新建未见 holdout，不能把这五块再次宣称为最终测试集。
2. V1 diarization 仍把说话人映射到整条 VAD 片段；V2-D 已用独立重叠时间轴修复数据表达，但现有人工标注刻意漏标电视声且 speaker 身份不穷尽，真实 DER/JER 暂时必须为 N/A。
3. `workflow-v2` 已持久编排 C→D→E 并与 V1 `daily-run` 分开；网页仍主要暴露 V1 一键入口，尚未加入原生分片会话选择和 V2 恢复按钮。
4. V2-E.0.2 已完成一次 Codex manual eval，但它不是可复现的固定模型质量基准；真实云端模型、数据保留策略、上下文上限和独立的幻觉/遗漏评测仍需在 V2-E.1 明确后才能调用。
5. 固定人物可以登记，但尚无通用的跨天人物候选匹配和人工确认流程。
6. schema v12 已新增不可变备份及逐文件证据；可变的只有当前校验/恢复状态。完成 run、原始模型输出和语义基础候选仍不可变。后续结构变化必须继续新增 migration，不能修改既有版本。
7. `daily-run` 已使用统一配置；旧的分步命令仍保留各自参数，后续可逐步接入同一配置层。
8. 已建立 Git 基线提交 `d9e9377` 和一键日记提交 `57cbe2e`；后续能力继续按可验证功能独立提交。

## 6. 本次盘点验证

- 可维护性重构阶段 0 的行为与依赖基线见 [阶段 0 基线](refactoring-phase-0-baseline.md)：重构前 79 个测试通过；新增 CLI smoke 和架构约束后 86 个测试通过，且 Ruff 对 `src`/`tests` 通过。本阶段没有加载真实模型或读取真实录音。
- `doctor` 全部通过：Python 3.12.10、FFmpeg/FFprobe、FunASR 1.4.4、ModelScope 1.39.1、pyannote.audio 4.0.7、PyTorch/torchaudio 2.9.1+cu128 和 RTX 5070 CUDA 实算正常。
- 原有 79 个单元测试在阶段 0 开始时全部通过，覆盖 schema 自动备份、冻结 ASR/token/source/diarization/attribution/semantic 防篡改、跨源引用、v10→v11 身份证据迁移、分片清单事务、相同静音内容的不同实例、session-native run、不可覆盖独立备份、清单外文件拒绝、篡改撤销恢复资格、production/shadow 准入、原音篡改预阻断、阶段复用、持久工作流、V2-C/D/E 与既有评测逻辑。V2-W.1 新测试只使用临时合成 WAV，没有读取或运行新增真实音频。
- 真实库已从 schema v3 升级到 v4，升级前自动保存一份 schema v3 SQLite 备份。迁移前后 V1 各表计数一致：423 个语音段、17 条人工身份标注、26 条声纹样本、10 个事件和 3 个历史 run 均保留。
- 当前 79,826,205 字节 Watch M4A 完整性状态为 `verified`；迁移和真实逻辑窗口解码前后 SHA-256 均为 `3503e63fc61b0b97a91d0ecb1ea925fbbd81ae021790134f538b04c7ccfd2d08`。
- 当前长录音可规划为 33 个无空洞的 5 分钟核心窗口并带 5 秒边界上下文；真实 10 秒核心窗口临时解码为 11 秒 WAV，退出后缓存已删除。
- 真实库已从 schema v4 升级到 v5，迁移前自动保存 schema v4 SQLite 备份；迁移前后 423 个语音段、17 条身份标注、26 条声纹样本、10 个事件和 3 个历史 run 计数一致。
- 已将现有前 15 分钟标注冻结为 277 条连续时间事实，并冻结当前 V1 的 971 条预测；新 benchmark CER 为 `0.1436`，与历史 `14.36%` 一致。
- 当前旧真值的 VAD/DER/JER/对齐指标明确为 N/A；合成穷尽真值测试已验证对应指标计算，待人工完成连续覆盖后才报告真实数值。
- schema v5→v6 升级前已自动备份真实 SQLite；升级后原音再次审计为 `verified`，79,826,205 字节和 SHA-256 均未变化。
- schema v6→v7 升级前已自动备份真实 SQLite；迁移前后 423 个 V1 片段、17 条身份标注、26 条声纹样本、10 个事件、165 个 ASR hypothesis 和 20,967 个 ASR token 全部不变，原音 SHA-256 仍为 `3503e63fc61b0b97a91d0ecb1ea925fbbd81ae021790134f538b04c7ccfd2d08`。
- Community-1 未缓存且没有标准 HF 登录或 `HF_TOKEN` 时会在联网/解码/创建 run 前立即终止，不产生空 run；登录后已完成 run 11，模型 revision 为 `3533c8cf8e369892e6b79ff1bf80f7b0286a54ee`。
- run 11 的 1,940 条 turns 均有完整原音 source trace，覆盖长度错误为 0；2,192 个 committed token 全部有归属决定，prediction set 15 已冻结。临时整段 WAV 已删除，原音字节数和 SHA-256 不变。
- V2-D.1 run 12 保留 run 11 的四个匿名 speaker 不变，truth set 3 只冻结 `17:00–17:14 = media_playback` 来源事实且不提供身份；原音再次审计为 `verified`。
- V2-D.3 首轮 top-12 人工审核确认 11 条父亲、排除 1 条；高/中对照共 11/11 命中，唯一错误是探索项。run 17 因此优先高/中对照，并自动继承同一输入、同一真值、同一绝对音频区间的人工结论；当前 11 条确认候选为 23.340 秒，加原种子共 29.310 秒。
- schema v7→v8 升级前已自动备份真实 SQLite；身份候选审核只写 `identity_candidate_reviews` 覆盖层，不改完成 run，不持久化 embedding。run 17 后永久原音仍为 79,826,205 字节且 SHA-256 不变。
- schema v8→v9 增加 `identity_reference_intervals`：只索引人物、人工结论、会话区间以及永久原音 SHA-256/坐标。当前父亲参考集为 11 条候选确认加 6 条原始真值，共 29.310 秒；另保留 2 条人工排除。它是可跨录音增长的临时参考集，不复制音频、不保存 embedding、不自动绑定身份。
- schema v9→v10 升级前已自动备份真实 SQLite；旧 run 18 因把 120 秒播放器限制当作语义边界而生成 35 个事件，现作为不可变偏差记录保留。V2-E.0.1 run 20 将同一 ASR run 10、diarization run 11 和 2,192 个 token 组织为 2 个完整对话、7 个仅本地低信息块和 3 个不可变候选；provider 计划为 1 个 98,600 字节任务。原文件仍为 79,826,205 字节且 SHA-256 不变。
- V2-E.0.2 run 21 完成首次 Codex manual replay；随后 provider 请求补全逐字段响应契约，因请求哈希变化而按不可变原则新建最终 run 22，没有覆盖 run 21。run 22 复用同一 ASR/diarization 证据，生成 9 scene、1 claim、0 action 和 7 unresolved。验证器没有把匿名簇当人物；唯一个人 claim 由 `mother` 区间身份和 `live_person` 来源共同支撑；纯媒体与未知来源段均未生成个人事实。
- Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B 已在当前 8 GB 5070 上以 BF16、batch=1 运行真实 40 秒片段并返回 71 个对齐 token；Fun-ASR-Nano 对同一片段返回独立假设和 90 个 CTC token。
- 首个完整五分钟 V2-C VAD-utterance 窗口已端到端完成：1 份主假设、1 份第二假设、236 个不可变对齐 token、1 条分歧记录、0 个低覆盖假设；每个 token 均有原始对象 SHA-256 和完整源时间覆盖。
- 当前 2 小时 44 分 35 秒录音已完成全量 V2-C run 7：33 份 Qwen 主假设、33 份 Fun-ASR 第二假设、6,147 个 token、16 个分歧窗口和 3 个低于 85% 对齐覆盖的非空假设；8 GB BF16 全程未 OOM，端到端耗时约 8 分 14 秒。
- 6,147 个 token 均有且仅有完整源范围，覆盖错误为 0，唯一源 SHA-256 仍为 `3503e63fc61b0b97a91d0ecb1ea925fbbd81ae021790134f538b04c7ccfd2d08`。
- 修正五分钟 transcript 区间过宽的 snapshot 适配器后，Qwen prediction set 3 的 CER 为 `0.3003`；V2-C.1 审计确认它不能与 V1 `0.1436` 作纯 ASR 排名。错误的 set 2 和 `0.7859`、以及后续 set 3 都不删除，作为不可变流水线审计记录保留。
- V2-C.1 在同一 46 条人工边界上重新运行三套模型并冻结 prediction set 4/5/6：SenseVoice/Qwen/Fun-ASR 原始 CER 为 `0.1593/0.2063/0.2585`，Qwen/Fun-ASR 的 ITN 等价 CER 为 `0.1984/0.2480`。
- Qwen 相对 SenseVoice 的 ITN CER 差值为 `+0.0392`，20 万次 paired bootstrap 95% 区间 `[-0.0085, 0.0956]`；旧样本有 V1 选择/边界/非盲偏置，不能外推为 Qwen 普遍更差。
- 已用不读取模型输出的固定算法从原音生成 30 分钟盲标任务，范围 `6102000–7902000 ms`，六个派生 WAV 均有 SHA-256；任务保持 pending，导入器会拒绝未完成或含模型/V1 字段的文件。
- 已增加仅加载盲标任务和派生 WAV 的本地专用网页：任意数量音频块的独立进度、播放器取点、可听清/无法听清语音成对标注、现场/电视媒体/重叠/不确定声源、编辑后自动撤销复核、最终声明锁定；旧标注按现场人声兼容，服务不连接模型结果数据库，保存不触碰原始 Watch M4A。
- V2-C.1 连续任务经人工抽听和 FFmpeg 复核为近静音，正式降级为环境负样本；不删除、不伪装成主 ASR CER 测试集。
- V2-C.2 已扫描整段不可变 PCM，仅按 RMS 活动、持续活动、P90/RMS 音量和语音频带能量排名，固定生成 `00:11–00:37` 之间十个相隔至少一分钟的一分钟块；总复核时长 10 分钟。
- V2-C.2 selection manifest 保存所有候选分数并绑定 SHA-256；导入器逐项检查窗口范围/排名/分数，benchmark 只计算十个 review-region，块间空隙中的预测不会被误罚。
- `pip check` 报告没有依赖冲突。
- 已修复 `audit-speakers` 更新标签后不刷新处理阶段摘要的问题，并将当前录音摘要修复为 115 个已分配片段、308 个 `unknown`。
- `daily-run 1` 已在真实长录音上复用 423 段 ASR、17 条人工身份标注并生成 10 个事件；当前严格行动规则没有在该录音中产生候选。
- 录音 1 的前 15 分钟真值已经完成首轮标注：93 个片段中纳入 87 个，46 个有可靠文字，72 个有人物/身份标签。
- 首份报告已建立：清晰片段 CER `14.36%`，说话人成对 F1 `0.543`；本人指标虽然为 `1.0`，但只有 2 个正样本，不能视为产品准确率。
- 本地网页为每段分别提供严格边界音频和前后 3 秒辅助上下文，避免把上下文文字写入真值。

## 7. V2 实施路线

完整设计、实体定义和验收条件见 [V2 质量优先架构与实施设计](v2-quality-first-architecture.md)。当前顺序已经确定：

### P0：不可变数据和可比较运行（V2-A 已完成）

1. 新增 schema v4，不修改既有 migration。
2. 将当前长录音映射为不可变 `source_object` 和 `recording_session`，原文件不移动、不改写。
3. 为原始对象增加完整性审计、备份状态和最后校验时间。
4. 以输入哈希、模型 checkpoint、代码版本和配置哈希共同决定 run 复用。
5. 停止新增长期整段 PCM，改用长文件逻辑窗口和可删除缓存。

### P1：连续时间评测（V2-B 核心已完成）

1. 将真值从 `segment_id` 解耦，锚定原始音频 SHA-256 和绝对时间范围。
2. 已增加 VAD Miss/FA、DER/JER、对齐误差和关键实体指标；V2-D 已提供 token-to-speaker 输出，speaker-attributed CER 还需穷尽 speaker 真值和对应 benchmark adapter，不能用整段单人标签伪造。
3. 保留 V1 SenseVoice 结果，建立同一输入的多 run 对比报告。

### P2：质量优先音频模型

1. Qwen3-ASR-1.7B 作为主 ASR 候选。
2. Qwen3-ForcedAligner-0.6B 输出字符/词级时间戳。
3. Fun-ASR-Nano-2512 生成第二假设和关键字段分歧队列。
4. Community-1 已生成可保存的重叠/exclusive speaker turns；Sortformer 因 4 人上限和非英语风险保留为待同真值比较的可插拔候选。
5. token 与说话人时间轴融合、来源审计和弱种子身份扩样队列已实现；当前会话停止继续硬挖，人物参考按永久原音坐标保存。下一步等新录音自然增加跨条件样本，再做跨天候选匹配与独立 holdout 验证。

### P3：云端语义和 Watch 同步

1. [x] V2-E.0.2 定义 episode/utterance/scene、四轨归属证据和供应商无关交换，不让 LLM 覆盖原始 ASR。
2. [ ] V2-E.1 在单独授权后接入真实云端 provider，评测事实一致性、遗漏、幻觉和行动项精度。
3. [x] 通用 5 分钟/任意长度 chunk manifest、幂等导入和跨块连续性检查；Watch 上传与断点传输客户端仍待实现。
4. 自动上传和手动点击同步共用同一协议；同步不阻塞既有本地音频与证据层。

## 8. V2-A 至 V2-E.0 交付结果与下一轮目标

V2-A/V2-A.1/V2-W.1：**“不可变原始对象、schema v12、多分片清单、可恢复备份、准入门与逻辑窗口”** 已完成。

完成条件：

- [x] 现有 2 小时 44 分原始音频路径和 SHA-256 全程不变。
- [x] schema v3 通过新增 migration 升级，V1 数据和人工标注保留。
- [x] 同一输入可以创建带输入指纹、模型清单、代码版本和配置哈希的独立 run。
- [x] 长录音可按逻辑窗口读取，不新增长期整段 PCM。
- [x] 临时窗口删除后可以只依靠原始 M4A 和数据库重新生成。
- [x] 五分钟或任意长度分片可通过清单适配器原子导入；相同内容哈希不再吞掉不同时间的真实静音实例。
- [x] V2-C/D/E 与 `processing_runs` 可只使用 `session_id`，不制造虚假的代表录音。
- [x] V2-W.0 在模型前复核清单/逐实例哈希并拒绝 gap、overlap 或篡改，进度和失败状态保存在 SQLite。
- [x] 原音独立第二份存储、当前字节复核和恢复演练机制；每条真实会话仍须由用户提供实际独立位置并通过 `session readiness`。

V2-B 已完成 schema v5、连续时间真值格式、旧标注迁移、不可变预测快照、VAD/DER/JER/对齐/实体指标和多 run 对比。当前标注的覆盖等级被真实保留，没有把稀疏 V1 标注误报为穷尽真值。

V2-C 已完成 schema v6、Qwen3-ASR-1.7B、Qwen3-ForcedAligner-0.6B、Fun-ASR-Nano 第二假设、边界 token 去重、分歧队列、失败续跑和 token 时间 benchmark snapshot。V2-C.3 已增加 FSMN proposal、Silero/相对 SNR/时长证据门控、推理 padding 与提交 core 分离，以及同时含 speech 和 committed token 的 v4 snapshot。默认 `auto` 会在 8 GB 上选择 batch=1、在不少于 14 GiB 时选择较高 batch；模型和 BF16 精度不变。

当前 V2-C.3 在五块开发集上达到总体 CER `92.27%`、现场 CER `116.18%` 和 VAD-F1 `61.42%`，但这五块已经参与阈值选择。V2-D 已完成数据结构、Community-1 backend、token 融合、CLI、全量 run 11 和 snapshot；下一步建立包含现场人物、电视/媒体声和重叠的穷尽小型真值，不能把现有五条人工标记包装成 DER/JER。

V2-E.0.2 已完成实现和真实 run 22 验收：启发式容器改称 episode，utterance 是 provider 唯一主文本，ASR/匿名声簇/source/identity 四轨证据互不替代，LLM 只生成带 utterance 引用的 scene/claim/action/unresolved。下一步确定真实云端供应商、模型上下文与隐私策略，实现 V2-E.1，并建立独立人工审核集衡量场景覆盖、事实一致性、人物归属、媒体误纳入、遗漏和幻觉。
