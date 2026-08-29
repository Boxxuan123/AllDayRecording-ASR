# V2-E.0.2：Episode 语义证据与可替换 LLM 接口

V2-E.0.2 冻结“音频模型证据如何进入语义模型”的最终本地契约。它不把工程窗口、静音间隔或匿名声簇包装成业务事实；本阶段先由当前 Codex 任务对真实脱敏文字包做一次人工 LLM 验收，项目运行时仍不调用云端 API。以后接入真实 provider 时只替换生成器，不改变证据实体、验证规则或审核流程。

## 1. 先定义任务，再定义输入

语义模型要完成四类任务：

1. 在连续录音上下文中区分家庭交谈、电视/媒体内容和其他语音场景。
2. 生成有 utterance 证据的场景摘要，而不是改写 ASR。
3. 提取“谁表达了什么”的 claim；主体不确定时必须保留 `unknown`。
4. 提取可能的行动项；没有可靠主体、时间或原文证据时不得自动成立，任何外部写入仍需人工确认。

因此输入不是普通文章，也不是一组固定 120 秒事件。唯一稳定层级是：

```text
session
└── episode            声学上下文容器，不是语义事件
    └── utterance      唯一的 LLM 主文本证据单元

LLM 输出
├── scene              语义场景/话题
├── claim              有主体和证据的陈述
├── action             待人工确认的行动候选
└── unresolved         无法可靠判断的歧义
```

## 2. Episode 不是“完整对话”

默认仍用 committed token 之间超过 180 秒的无文字间隔形成 episode 候选，但它只负责保存完整上下文。电视节目可以持续填补静音，从而把家庭吃饭谈话和媒体内容连接进同一 episode；因此 episode 不代表一次交谈、一个话题或一个事件。

- episode 没有固定时长上限；墙钟时长不决定是否分片。
- 是否需要 provider 分片只由实际序列化 token/字符量和所选模型上下文决定。
- 分片只能沿 utterance 边界进行，带相邻 utterance 重叠，并保留同一 parent episode key。
- provider 返回后必须先恢复 parent episode，再生成日级结论；传输分片永远不是 scene。
- 最多 120 秒的 WAV 仍只用于网页回听，不参与 episode 或 scene 边界。

## 3. 四条互不替代的证据轨

每个 utterance 同时携带以下字段：

| 字段 | 来源 | 能说明什么 | 不能说明什么 |
| --- | --- | --- | --- |
| `text` / `asr` | V2-C committed 主假设和分歧证据 | 说了哪些字、哪里存在第二假设 | 不能证明说话人或内容属实 |
| `voice_cluster` | V2-D token-to-speaker | 局部声音连续性、换人和重叠提示 | 匿名簇不是人物；污染簇不能全局绑定身份 |
| `source` | 冻结人工 speech-source 区间 | `live_person`、`media_playback`、`mixed_live_media` 或 `unknown` | 稀疏区间外不能外推 |
| `identity` | 冻结人工 speaker 区间或人工确认身份参考 | `self`、`mother`、`father` 等区间级人物证据 | 不能把短区间标签传播到整个匿名簇 |

解析规则固定如下：

- `voice_cluster` 始终标记 `identity_safe=false`；若 V2-D.2 已确认污染，还要附 `contaminated=true`。
- `source` 和 `identity` 都保存全部重叠证据及覆盖比例；只有一个标签覆盖 utterance 至少 50%，且领先第二名至少 15 个百分点时才给出 resolved label，否则为 `unknown`/`ambiguous`。
- 旧人工 `speaker=tv` 只转换为 `source=media_playback`，不创建名为 tv 的人物。
- `mother/father/self` 等人工 speaker 区间同时提供 `source=live_person` 证据。
- 无覆盖证据时明确写 `unknown`，允许 LLM提出假设，但不能把假设升级为事实。
- 重叠讲话、不可靠归属和 ASR 分歧分别保留，不能相互抵消。

## 4. 本地底账与 provider payload

### 本地不可变底账

本地底账继续保存全部 committed token、数据库 token ID、永久原音 SHA-256/坐标、完整 source/identity 证据、V2-C/V2-D run、ASR 第二假设和低信息排除块。它用于重放、验证和回听，不发送给 provider。

### Provider 唯一主输入

provider 只接收按时间排序的紧凑 utterance 记录：

```json
{
  "episode_id": "episode-0001",
  "utterances": [
    {
      "id": "episode-0001:u-0138",
      "start_ms": 684200,
      "end_ms": 688500,
      "text": "哎我替小儿子包一个吧",
      "voice_cluster": {
        "label": "SPEAKER_03",
        "assignment": "primary",
        "identity_safe": false,
        "contaminated": true
      },
      "source": {
        "label": "live_person",
        "resolution": "human_interval",
        "evidence": [{"label": "live_person", "overlap_ratio": 0.94}]
      },
      "identity": {
        "label": "mother",
        "resolution": "human_interval",
        "evidence": [{"label": "mother", "overlap_ratio": 0.94}]
      },
      "uncertainty": {
        "speaker_uncertain": false,
        "overlap": false,
        "asr_alternative_available": false
      }
    }
  ]
}
```

固定约束：

- 主 ASR 文本只在 utterance 中出现一次，不再额外发送一份无 speaker 的拼接 transcript。
- 第二 ASR 只放在独立 `asr_alternatives`，并明确标为同一时间范围的备选证据，不是额外对话。
- provider 不接收音频、路径、文件名、原音哈希、数据库/录音/session ID、数据库 token ID 或声纹 embedding。
- episode 内的语气词保留上下文作用；与其他内容相隔超过 episode gap 的纯语气词块保留在本地 `excluded_blocks`，不单独发送。
- 不为节省成本删除有用证据；同时也不通过重复三份相同文本制造注意力污染。

## 5. LLM 输出契约

响应必须包含：

```json
{
  "daily_summary": {
    "title": "当天语义摘要",
    "body": "只总结有证据内容，并说明覆盖局限",
    "evidence_scene_ids": ["scene-0001"]
  },
  "scenes": [
    {
      "id": "scene-0001",
      "episode_id": "episode-0001",
      "type": "family_conversation",
      "start_ms": 684200,
      "end_ms": 750000,
      "title": "吃饭时的家庭交谈",
      "summary": "……",
      "participants": ["mother", "father", "unknown"],
      "evidence_utterance_ids": ["episode-0001:u-0138"]
    }
  ],
  "claims": [],
  "actions": [],
  "unresolved": []
}
```

验证器必须拒绝：

- 引用不存在或超出 scene 时间范围的 utterance；
- 把 `media_playback` 内容写成个人经历；
- 仅凭匿名 voice cluster 声称真实人物身份；
- 主体没有身份依据却生成归属于具体人物的 claim/action；
- 没有 evidence utterance 的事实或行动；
- 试图覆盖 ASR、说话人时间轴或旧 semantic run；
- `requires_human_confirmation=false` 的外部行动候选。

scene 可以引用 source/identity 均未知的 utterance，但必须在 summary 或 `unresolved` 中保留歧义。claim/action 的归属规则更严格，不满足时主体只能是 `unknown`。

## 6. 当前 Codex 人工 LLM 验收

用户已明确授权当前 Codex 任务读取最小化文字证据并充当一次人工语义模型。该测试与正式 API 严格区分：

- 项目运行时不发起网络请求，也没有 API key。
- Codex 只读取 provider 将来会看到的最小化文字包，不读取或上传原始音频字节。
- 生成结果标记为 `provider=codex_manual_eval`、`model=codex-session-unversioned`，不能冒充可复现的固定模型 API run。
- 请求、人工生成响应、验证结果和候选仍按不可变 semantic exchange 保存。
- 这次结果用于发现契约、归属和媒体过滤问题，不作为模型质量基准；正式 provider 接入后必须在同一输入上重新跑评测。

官方 OpenAI 文档把 Codex用于分析、测试和迭代改进列为支持的工作方式；本项目仍把当前任务中的生成视为人工 record/replay，而不是隐藏的运行时 API。[OpenAI Codex use cases](https://learn.chatgpt.com/use-cases)

## 7. 历史版本和当前基线

| run | 结论 |
| --- | --- |
| 18 | 错把 120 秒播放器限制变成 35 个语义事件；保留为不可变偏差记录 |
| 19 | 传输分片未完整重聚合，被验证器拒绝，没有 semantic exchange |
| 20 | 修复 120 秒硬切，形成 2 个无时长上限容器；但误称“完整对话”，且 payload 尚未接入 source/identity |
| 21 | 首次 Codex manual record/replay；语义结果通过验证，随后发现 provider 请求还应内嵌逐字段响应契约 |
| 22 | 补全自描述响应契约后的最终 V2-E.0.2 replay；2 个 episode、9 个 scene、1 个 claim、0 个 action、7 个 unresolved |

run 20 的 98,600 字节中，主 episode 实际 transcript 只有 2,131 个汉字（约 6.4 KB）；288 个结构化 utterance 约 70 KB，8 个窗口级双 ASR 备选约 18.6 KB。V2-E.0.2 优化的是语义清晰度和注意力重复，不以压缩成本为目标。

run 22 的脱敏 provider 请求把同一批 committed token 组织为 2 个 episode、292 个主 utterance 和 9 个双 ASR 备选窗口。第一 episode 的来源分布为 `live_person=48`、`media_playback=39`、`unknown=201`，身份分布为 `father=13`、`mother=18`、`self=2`、`unknown=255`。Codex 只把 `mother` 身份区间直接支撑的“不能吃螃蟹，只能吃虾和鱼”保存为个人 claim；纯媒体段没有生成个人事实，整段没有硬凑 action。后半段来源区间缺失但文字像电视旁白的内容只生成低置信 scene，并在摘要中保留不确定性。第二 episode 的四个短句与备选 ASR 不一致，完整保留在 unresolved。

校验器已实际验证：episode 超过 120 秒仍不被当作多个语义场景；匿名 voice cluster 即使被 D.2 标记为污染也不能成为身份；无区间身份支撑的具体人物会被拒绝；纯媒体证据生成个人事实会被拒绝；action 必须标记人工确认。网页展示 episode 容器与 scene/claim/action 候选为两层，不再把容器标题冒充语义结果。

## 8. V2-E.1 API 接入条件

1. 明确供应商、具体模型、上下文上限、数据保留策略和上传字段，首次调用前单独授权。
2. provider 直接复用 V2-E.0.2 request/response schema；不得重新解释 episode 或 voice cluster。
3. 优先一次提交完整 episode；只有序列化输入实际超限时才做带重叠 utterance 分片和 parent 聚合。
4. 保存原始云端响应、模型版本、请求参数和聚合过程，不覆盖任何既有证据。
5. 用人工审核集报告 scene 覆盖率、claim 事实一致性、无证据事实率、人物归属错误、媒体误纳入率和 action precision/recall。
6. 所有真实日历、待办或外部系统写入继续要求人工确认。
