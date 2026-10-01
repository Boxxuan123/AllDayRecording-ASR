# Speaker Turn / Blind Query Boundary Audit — 2026-10-01

本轮仅定位。发现了两条可复现的结构机制：ASR token 投影可跨过已检测的短 speaker turn；Blind query 会在 8 秒上限或 capture 文件边界裁短仍在继续的 utterance。但最新三个 `mixed_overlap` 任务的混合说话首错层仍为 **UNKNOWN**，不能把这两条机制直接当作它们的主因。

生产 VAD、diarization、track/cluster builder、query builder、matcher、enrollment 和审核 UI 均未修改。正式 SQLite 使用 `mode=ro`、`query_only=ON`，读取前后数据库及生产 Python 源码哈希一致。只新增诊断工具、工具测试和本文；识别信息与原始链路留在 ignored `outputs/`。

## 范围与证据口径

- 最新 active Blind：3 个任务、15 个源窗口，全部追踪；3 个最新提交均为 `mixed_overlap`，没有 `boundary_cross` 或 clean Blind 对照。任务来自一个已处理会话，不能外推总体发生率。
- 历史纯度库：169 个最新有效提交，包含 44 mixed、7 boundary、108 clean、10 uncertain。选取 17 个支持案例，包括全部 7 个 boundary、5 个有 Enrollment prototype provenance 的 mixed、2 个 wrong-primary 和 3 个 clean 对照。
- 同一 Blind processing run：自动扫描 A 长段、B≤2s、A 恢复、两侧 gap≤1200ms 的 adjacent exclusive-turn 候选，并挑选 7 个作支持案例。共 27 个案例；支持样本不混入最新 Blind 分母。
- 每个案例保留 raw 文件/校验和、capture 坐标映射、VAD、ASR token/utterance、regular/exclusive diarization、track/cluster、source windows、事件及播放映射。边界附近保留至少 ±5s 的层证据。

现有人工标签没有逐窗口子类型、短 B 的准确起止或原音逐 turn 真值。本轮没有独立声学复听，因此 `raw_speech_spans` 明确为空。原音可用且校验和一致，**可用音频不等于已经听辨其真实 speaker**。未生成新的手机任务，也未改写既有人工 verdict。

## 最新 Blind 的主因归属

下表统计“已确认是最新 mixed 首次错误的层”；0 表示没有确认归属，不能解释为排除该机制。固定窗口裁短作为独立次级发现另表列出。

| Root cause | 已归属任务 | % | 影响模型 | 影响审核 |
|---|---:|---:|---|---|
| Diarization speaker error | 0 | 0 | 未定 | 未定 |
| A-B-A downstream merge | 0 | 0 | 未定 | 未定 |
| Fixed window truncation 是 mixed 首因 | 0 | 0 | 未定 | 未定 |
| Review padding only | 0 | 0 | 未发现额外 padding | 未发现额外 padding |
| 人工确认的 true overlap | 0 | 0 | 未定 | 未定 |
| 混合源的首次错误层 UNKNOWN | 3 | 100 | 同源 mixed 内容进入 query | 同源播放 |

三个任务均有 regular diarization 的双人重叠预测，但这不是人工确认的 simultaneous overlap。15 个窗口没有完整位于内部的其他 exclusive speaker turn；两个窗口仅有 token 多数归属造成的极短起始边缘跨 label。不能用它们解释“大段两人串行说话被合并”，也不能从 exclusive 单 label 推断原音真实单人。

`model_query_affected=true` 的依据是：用户在与模型完全相同的源窗口上给出了 mixed verdict。它表示混合内容进入模型，不代表已测得身份决策错误或性能下降。三个任务均不是已证明的 `review_only_affected`。

## A-B-A / backchannel

这里的单位是**模型预测的 A-B-A 候选**，不是人工确认的 backchannel。短 turn 可能包含模型抖动；没有 ASR 文本仍能发现候选。

| 预测链路状态 | 候选数 |
|---|---:|
| 总计 | 105 |
| B 被检测，但后续 A utterance 跨过整个 B | 18 |
| A、B、恢复后的 A 均有投影保留 | 29 |
| B label 被保留，但一侧 A 没有匹配投影 | 20 |
| B 未投影，且没有 A 连续范围跨过它 | 38 |
| Diarization missed | 不可测 |

B 时长分布：≤300ms 51；301–500ms 14；501–1000ms 19；1001–2000ms 21。

18 个重并候选中，9 个 B 内没有 primary ASR token，另 9 个虽有相交 token，但 token 多数归属不产生 B label。**这 18 个重并 turn 均没有完整进入此次 15 个 Blind 源窗口**。所以“存在下游重并”已证，“它主要解释最新 mixed”未证。

第一处把分离范围变成连续 hull 的函数是 [`native_projection.py::_group_utterances`](../src/allday_asr/v3/adapters/models/native_projection.py#L70)。它只比较相邻 token 的已归属 label、1200ms gap 和 30s 总长；没有回查这段 gap 内的 diarization turn。若 B 的 token 缺失或被归给 A，两个 A token 可以变成一个连续 A utterance，源音频里的 B 被夹入。这是 **CROSS-SPEAKER_GAP_FILL / BACKCHANNEL_REMERGED_DOWNSTREAM**，发生在 track/query 选择之前。

[`_speaker_for`](../src/allday_asr/v3/adapters/models/native_projection.py#L51) 为整枚 token 汇总各 label 的时间交集，选择达到阈值的多数 label；不在 speaker 边界切开 token。调用入口 [`NativeModelPipelineAdapter._project_utterances`](../src/allday_asr/v3/adapters/models/native_pipeline.py#L262) 只使用 exclusive turns，再将 attributed tokens 分组。枚举 `ASR_SEGMENTATION` 在本审计中也覆盖这段 **ASR token→utterance 投影**，不是声称声学 diarization 没检测到 B。

300/500/1000ms 合成 A-B-A 复现：B token 存在时保留三段；B token 不存在时三个时长均被 `_group_utterances` 合成 A 连续范围。Enrollment fact union 对这三个正 gap 均保持两段。该合成探针只验证代码机制，不计入真实案例。

没有可依据 ASR 确认的 laughter 候选。不能据此判定原音没有笑声，尤其无 token 的短 B 完全可能被漏计；需要人工真值后才可标为 `LAUGHTER_BACKCHANNEL`。

## “说一半截断”

| 层/机制 | 已确认结构裁短的窗口 | 说明 |
|---|---:|---|
| VAD 错切连续原音 | 未定 | 缺少原音连续讲话真值 |
| ASR segmentation 首次语义截断 | 未定 | 不能用窗口长度代替语义判断 |
| Query 的固定 8s 上限 | 8 | 原 utterance 结束时间更晚 |
| Query 的 capture 文件末端 | 5 | 首个文件取完即 break，未继续取后续文件 |
| Review 新增裁短/padding | 0 | 与已存 query windows 一致 |

13/15 个窗口在原 utterance 结束前被裁短，涉及全部三个任务。13 处均有同 label exclusive turn 与 VAD speech range 跨过裁切点，2 处甚至切在 aligned token 内。可高置信定位“首次结构裁短”在 query construction；仍不能把 13 个结构裁短全部宣称为人工确认的“半句话”。这不是 13 个独立用户异常。

位置：[`blind_queries.py::automatic_queries`](../src/allday_asr/v3/adapters/sqlite/blind_queries.py#L18)，第 38 行 `min(utterance.end, capture.end, start+8000)`；第 49 行首次有效 capture 后 `break`。因此既有固定 8s 问题，也有短于 8s 的 file-edge 问题。后半 utterance 仍在时间线里；它不会由此函数自动作为下一段继续加入同一 query。

## 模型 composition、track 与播放

1. **Track is identity grouping。** [`processing_service.py`](../src/allday_asr/v3/application/processing_service.py#L290) 按 run+speaker label 创建一个 track，整个 run 内该 label 的 utterance 都归它。track 不是一个连续 speaker turn；cluster merge 移动 membership，不生成音频时间 hull。
2. Blind builder 从原始 automatic track 的最长 12 个 utterance 候选里，选至多 5 个不重复源窗口；每个窗口≤8s、≥800ms。它没有使用 Enrollment 的 `compute_plans`、40s contiguous-range chunk/union 算法。相似的“最长五、8s”选择策略不等于相同函数。
3. [`BlindValidationService.run_once`](../src/allday_asr/v3/adapters/blind_validation.py#L162) 将每个 window 单独作为 embedding input，验证实际 representatives 一致，再 [`unit(mean(window_vectors))`](../src/allday_asr/v3/adapters/blind_validation.py#L178)。保存的每-window vector 与 query vector 可精确复算；不是对 session_start/end 包络的一大段连续音频提取 embedding。
4. [`_group_events`](../src/allday_asr/v3/adapters/blind_validation.py#L214) 只把 related query 分成 evaluation component，保留一个 canonical query；不拼接 component 的源范围。当前三个 event 各含一个 query。same-track 5s / same-cluster 2s 是统计去相关阈值；当前 query 没有 cluster_id，cluster 分支不解释当前样本。
5. [`blind_reviews.py::phone_tasks`](../src/allday_asr/v3/adapters/sqlite/blind_reviews.py#L18) 原样将 query windows 映射到 representative_clips。 [`review_audio.py::audio_plan/concatenate`](../src/allday_asr/v3/interfaces/review_audio.py#L8) 按列表顺序剪取并直接串接，总长度是窗口时长之和；不包含窗口间的原音、不添加 silence 或原音 padding。排序依据是候选优先级而非完整对话时间顺序，因此能产生突兀跳转的 montage，但模型也使用相同窗口。
6. [`DeviceReviewService.audio/render_audio`](../src/allday_asr/v3/interfaces/device_reviews.py#L304) 只允许 `speaker_profile_purity` 申请 ±4s context；Blind 不允许。普通 response 是已渲染 WAV 的 `[0,total_ms]`。手机 `phone/src/main/ets/v3/data/PhoneV3ReviewAudioPlayer.ets:58` 播放 response 的 start 和 duration；`PhoneV3UseCases.ets:730` 普通 sample request 不申请 context，`PhoneV3PurityReviewPage.ets:242` 对 Blind 隐藏 context 按钮。手机仓库只读检查。

## 阈值与范围变化

| 层 | 阈值/范围 | 代码位置与意义 |
|---|---|---|
| VAD/FSMN proposal merge | gap≤600ms；合并后≤30s | `speech_gate.py:23,74`；speaker agnostic speech grouping；单条过长 proposal 本身不在此函数切成 30s |
| VAD inference/output context | 750ms / 500ms | `speech_gate.py:80,119`；ASR 的 context 不等于 review padding |
| Silero 配置 | min silence 250ms、min speech 100ms、overlap gate 500ms | 持久化 ASR model manifest；不是 speaker-turn 保留承诺 |
| ASR 分析窗口 | core 300s，context 5s | `native_pipeline.py:435` 与持久化 hypotheses；未证明是此次 query 裁短首因 |
| Token speaker assignment | 多数时间占比≥0.5 | `native_projection.py:51`；本地配置 replay 精确匹配 Blind transcript |
| Utterance group | gap≤1200ms；总长≤30s | `native_projection.py:70`；缺 B token 时会填跨 speaker gap |
| Automatic Blind / automatic prototype clips | 单窗≤8s；≥800ms；最多5；最长12候选 | `blind_queries.py:31,38,50`；`people_repository_analysis.py:64,84` 独立实现相近选择策略 |
| Enrollment fact union | 只合相同 media 的重叠或相接范围；正 gap 不合 | `annotation_sample_plan.py:83`；无 300/500/1000ms gap tolerance |
| Enrollment chunk / selection | 单 contiguous union 前40s；按8s分块；最长5个 | `annotation_sample_plan.py:100,102,115`；不在 Blind query 调用路径 |
| Independent event relation | 同track≤5s、同cluster≤2s；或源范围重叠 | `blind_scoring.py::related`；evaluation grouping，不构造音频 |
| Review montage | 最多5窗；总长≤40s | `review_audio.py:8`；40s 是总长校验，不是把 gap 填成连续40s |
| Review context | 仅 purity review ±4s；Blind 0ms | `device_reviews.py:304,334`；不是最新 Blind 混合的 padding 原因 |

## Enrollment 共因与独立问题

支持样本中有两个 Enrollment 源范围跨过保存的其他 exclusive speaker turn；其中一个是多次短 turn 交替，另一个是 A-B-A。它们的 automatic utterance hull 已经如此，后续 person fact/sample/prototype 继续使用源范围。**共享 ASR projection 边界丢失机制有实际 provenance 支持**，并非 Enrollment union 把正 gap 再填起来。但这些病例不能证明最新三个 Blind mixed 的首因；另一个历史人物的抽样 mixed 源仅看到单 exclusive label，声学漏分和真正同时讲话尚不能区分。

另一个 reviewed clean-but-wrong-primary 单窗已有错误目标 person fact，然后进入两次采样 aggregate 与人工确认。它与短 B merge 是不同授权链问题；单一 coherent speaker 的错误姓名归属不能用纯度或 A-B-A 边界修复来解释。

历史 purity 审核允许 target/context 两种听法，数据库没有记录实际播放选择。其 mixed/boundary verdict 对应何种播放范围不能追认，不能用它估算 padding-only 的比例。

## Q1–Q8

| 问题 | 结论 |
|---|---|
| Q1 两人合并主要首发在哪层？ | 最新3个 mixed 首错 **UNKNOWN/LOW**。已证下游 gap-fill 在 ASR token→utterance projection；没有证据说它是最新样本主因。 |
| Q2 B漏检还是重并？ | 已检测的候选中存在18个重并；diarization missed 不可测。不能断言两者总体比例。 |
| Q3 半句截断在哪层？ | 首次结构裁短在 Blind query 的8s/file-edge `min`；13处上游 utterance、VAD、同label turn仍继续。人工语义确认未完成。 |
| Q4 true overlap / A-B-A / boundary 各占多少？ | 现有 coarse truth 不可识别。regular预测3/3不是真 overlap比例；105个模型候选不是人工backchannel数。 |
| Q5 影响模型还是仅手机？ | 3个mixed-verdict query 的同源内容进入 CAM++；没有额外Blind padding。固定范围裁短也影响模型输入。未测匹配性能损失。 |
| Q6 与Enrollment有共因？ | 部分Enrollment有相同 projection hull证据；最新Blind mixed 的共因仍未定。clean wrong-primary为独立人名授权链。 |
| Q7 需要换diarization吗？ | **NOT YET JUSTIFIED**：下游有已证边界丢失，且无逐turn人工真值支持漏检率/模型替换收益。 |
| Q8 下一步幅度？ | **局部重构**：turn保留、token边界、source-window选择之间需一致约束；仅调一个gap阈值不能解决缺token与file-edge裁短。 |

## 最多三项后续建议（未实施）

- **P0**：让 ASR attribution/grouping 与 clip selection 保留 diarization turn 边界；禁止没有 B token 就跨过 B 构造连续 A hull。先用已保存的18个候选做回放回归，保留声学真值未知的标记。
- **P1**：将8s/capture文件裁切改为边界感知的 source-window计划，同时保留逐窗审核定位与模型composition映射；先处理13个结构裁短，不把 montage 当连续原对话。
- **P2**：补齐逐turn/逐窗 subtype 真值及 target/context 使用记录，再测 missed-B、laughter、true overlap；只有对照实验表明模型本身是主瓶颈才开展替换实验。

## 可重复验证

```powershell
.venv/Scripts/python.exe tools/audit_speaker_turn_boundaries.py
.venv/Scripts/python.exe -m py_compile tools/audit_speaker_turn_boundaries.py tests/test_speaker_turn_boundary_audit.py
.venv/Scripts/ruff.exe check tools/audit_speaker_turn_boundaries.py tests/test_speaker_turn_boundary_audit.py
.venv/Scripts/python.exe -m unittest discover -s tests -p test_speaker_turn_boundary_audit.py -v
git check-ignore outputs/speaker-turn-boundary-audit-20261001/cases.json
```

6项诊断测试覆盖：正gap不union、串行与预测同时重叠区分、B保留/被跨过区分、三个短turn阈值、索引attribution与原函数一致、只读SQLite拒写且文件哈希不变。数据库测试只使用独立临时fixture，不针对正式数据库尝试写入。生产无需新增测试。

正式输入稳定时，两次运行的全部七个诊断输出文件字节哈希一致。具体输入哈希、private schema、逐例first_bad_layer/confidence、边界证据与验证记录保存在 `outputs/speaker-turn-boundary-audit-20261001/`，不提交。脚本不会创建生产服务、运行新模型、写正式数据库或生成手机审核任务。已有未提交修改不包含在本轮提交中。
