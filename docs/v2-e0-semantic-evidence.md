# V2-E.0.1：完整对话优先的本地语义证据层

V2-E.0.1 先把“音频模型结果如何安全交给未来云端 LLM”做成稳定的数据契约，不在本阶段调用云服务，也不假装本地规则具备语义理解能力。它读取 V2-C committed token 和 V2-D token-to-speaker 归属，在本地保存完整可追溯底账，同时另外生成一份最小化的云端传输计划。

旧 V2-E.0 把 120 秒播放器上限同时当作语义事件上限。真实录音因此被切成 35 个事件，其中纯“嗯”等低信息块也被当成与长对话同级的 LLM 输入。这是数据模型错误，不是 LLM 参数问题。V2-E.0.1 已把完整对话、provider 传输任务和播放器切片分成三个独立层次。

## V2-A 至 V2-D 边界审计

本轮逐层检查“工程窗口是否被误当成上层事实”，结论如下：

| 阶段 | 有限窗口的用途 | 上层连续性如何保留 | 是否存在同类错误 |
| --- | --- | --- | --- |
| V2-A | 5 分钟 core + 两侧上下文用于只读解码和未来 Watch chunk 装配 | session/source graph、绝对时间和 SHA-256 连续；窗口可重建 | 否 |
| V2-B | review-region 只界定人工评测覆盖范围 | `coverage` 明确区分 sparse/exhaustive；区间外不被解释成静音或错误 | 否；旧 V1 条件化边界偏差已单独披露 |
| V2-C | 五分钟推理窗、VAD 候选和最长 30 秒 utterance 是模型推理单元 | 5 秒上下文、token 中点落 core 才提交，最终回到统一 session 时间轴 | 否；边界仍可能影响 ASR 质量，但没有被声明成语义事件 |
| V2-D | 90 秒候选和 120 秒 WAV 用于排序、导航和回听 | Community-1 在整段 session 上推理；regular/exclusive turn 是模型输出，身份真值仍按绝对区间保存 | 否 |
| 旧 V2-E.0 | 120 秒原本只是网页可播放上限 | 没有保留完整对话实体，直接产生 35 个语义事件 | **是，已在 V2-E.0.1 修复** |

这里的“否”不代表 A–D 已经达到最终精度，只表示它们没有犯“把播放器/批处理限制伪装成业务事实”这一类错误。V2-B 的公平性、V2-C 的未见 holdout 和 V2-D 的穷尽 speaker/media 真值仍是独立未完成事项。

## 三层数据契约

### 1. 本地证据底账

本地底账保留全部 committed token、数据库 token ID、永久原音 SHA-256/坐标、说话人归属、不确定性、ASR 第二假设和被排除的低信息块。它用于重放、审计和人工回听，不发送给 provider。

### 2. 云端传输计划

provider payload 只包含：

- 录音日期、时区和总时长；
- 完整对话候选的起止时间、匿名 speaker、逐 utterance 文本和不确定性；
- 与相应范围重叠的 ASR 备选文字；
- LLM 必须返回的 evidence key 和输出约束。

它不包含音频、路径、文件名、原音哈希、数据库/录音/session ID、数据库 token ID 或声纹 embedding。

默认以 token 间 180 秒无文字间隔作为“对话候选”边界。这个边界明确标为启发式候选，不是真值；对话总时长没有硬上限。纯语气词或信息字符少于 4 的块仍留在本地底账，但不进入 provider payload。

当前传输预算是每个计划任务最多 200,000 字符，只用于匹配未来所选云模型的上下文。如果单个完整对话真的超限，才按 utterance 边界分片，并让相邻片保留 6 个 utterance 重叠；所有片都保留同一 parent conversation key，返回后必须重新聚合成一个对话。分片本身不是语义边界。

### 3. 网页回听片

网页最多一次生成 120 秒本地 WAV。一个 37 分钟对话可以对应 19 个回听片，但仍只有一个语义候选和一个对话 evidence key。播放器限制不会改变 provider payload 或 LLM 输出粒度。

## 数据流

1. 默认选择最新完成的 V2-D run，并严格采用它引用的 V2-C run；CLI 也可固定二者。
2. 只读取 V2-C committed 主模型 token；每个 token 必须可追溯到永久原音对象和时间坐标。
3. 先用 180 秒 token gap 形成无总时长上限的对话候选，再在对话内按 speaker 和 2.5 秒间隔组织 utterance。
4. 信息不足的块只写入本地 `excluded_blocks`，不会悄悄删除，也不会进入 LLM 请求。
5. 完整对话优先装入 provider job；只有超出传输预算时才做带重叠的工程分片。
6. mock 返回日级说明和逐完整对话占位卡。当前 `facts=[]`、`actions=[]`，不把规则文字伪装成日记事实。
7. 本地底账、最小化 provider payload、响应和候选分别冻结；数据库触发器禁止更新或删除。人工确认、驳回和文字修订只追加 revision。

schema v10 继续使用：

- `semantic_exchanges`：一次 run 的不可变本地底账/响应、provider、模型、哈希和隐私声明；
- `semantic_candidates`：不可变日级/完整对话/事实/行动候选及证据；
- `semantic_candidate_revisions`：人工确认或驳回后的追加修订历史。

## 使用

```powershell
# 默认使用最新匹配的 V2-C/V2-D 完成 run
allday-asr semantic-v2 build 1

# 固定输入 run，或调整候选/传输参数
allday-asr semantic-v2 build 1 --asr-run 10 --diarization-run 11 `
  --conversation-gap-seconds 180 --min-informative-chars 4 `
  --max-llm-request-chars 200000

# 查看最近证据包
allday-asr semantic-v2 status 1

# 在本地网页生成、分片回听和审核
allday-asr web
```

网页“语义证据”页会分别显示完整对话数、本地排除块数、计划请求数、token 数和隐私边界。每张完整对话卡可以切换 120 秒回听片；修改标题或正文后选择“确认”或“驳回”，不会改变基础候选。

## 真实长录音验证

录音 1 的修正版结果为 run 20：

| 项目 | 结果 |
| --- | ---: |
| 输入 | V2-C run 10 + V2-D run 11 |
| committed token | 2,192 |
| 可提交完整对话 | 2 |
| 本地保留、不发送的低信息块 | 7 |
| provider 计划任务 | 1，98,600 字节，包含 2 个完整对话 |
| 候选 | 3（1 个日级 + 2 个完整对话） |
| 主对话 | 00:00:53.280–00:38:12.430，共 37 分 19.150 秒、288 个 utterance、19 个回听片 |
| 第二对话 | 01:01:41.930–01:01:56.720，共 14.790 秒、4 个 utterance |
| facts / actions | 0 / 0 |
| 网络访问 | 否 |
| provider 收到音频/路径/原音哈希/数据库 token ID | 均为否 |
| 原 M4A | 79,826,205 字节，SHA-256 不变 |

本地底账 SHA-256 为 `4062a7917fcbb304dfc1d72595b2bf1266bad6c139c91dd37ae30733b2ded484`，provider payload SHA-256 为 `91b18e71a06423830da1f9093827f2cbc66fbde8a3d4c33cc95dd7b40935d070`，mock 响应 SHA-256 为 `b696714047253a9141de575ab9a4059e40e5c3b64b5c4b201b2738a8e0665b87`。私有完整清单写入 `outputs/session-000001/semantic-v2e0/run-000020.json`，该目录不提交 Git。

旧 run 18 仍以不可变历史保留，用来证明 120 秒上限造成了 35 事件偏差。run 19 在首次升级验证时因传输分片没有完整重聚合而被完整性校验拒绝，没有创建 semantic exchange；失败记录也保留作审计。run 20 修复后通过同一校验。

## V2-E.1 的进入条件

下一阶段可以增加真实云端 provider，但必须继续遵守同一边界：

1. 明确供应商、具体模型、上下文上限、数据保留策略和上传字段，首次调用前单独授权。
2. 默认只上传最小化文字证据；原音上传必须是独立、显式选择，不能由 provider 隐式读取本地路径。
3. 优先一次提交完整对话；只有实际 provider 上下文不足时才启用带重叠传输分片，并在生成日级结论前先做 parent conversation 聚合。
4. 响应中的每个事实、日程或行动必须引用 conversation/utterance evidence key；无证据输出不能进入确认队列。
5. 保存原始云端响应、模型版本、请求参数和聚合过程，不覆盖 ASR、说话人时间轴或既有语义 run。
6. 用人工审核集评测事实一致性、信息召回、无证据事实率和行动项精度后，再接入 Watch 同步后的日常批处理。
