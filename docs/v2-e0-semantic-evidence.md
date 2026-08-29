# V2-E.0：本地语义证据层

V2-E.0 先把“音频模型结果如何安全交给未来云端 LLM”做成稳定的数据契约，不在本阶段调用云服务，也不假装本地规则具备语义理解能力。它读取已完成的 V2-C committed token 和 V2-D token-to-speaker 归属，生成一份可重放、可审核、不可覆盖的本地证据包。

## 当前边界

- `provider=local_mock`，`network_access=false`；没有云端或本地 LLM 推理。
- 请求不含音频字节、文件路径、文件名或声纹 embedding，只含转写、时间、说话人归属、不确定性、token ID，以及原音 SHA-256/时间坐标。
- mock 只生成时间标签和文字预览；`facts=[]`、`actions=[]`，不会把占位内容伪装成日记事实。
- V2-C/V2-D 输出和语义交换完成后不可修改；人工编辑以追加 revision 保存。
- 外部日历、待办或其他系统写入不属于 V2-E.0，未来仍必须经过人工确认。

## 数据流

1. 默认选择最新完成的 V2-D run，并严格采用它引用的 V2-C run；也可以在 CLI 明确指定二者。
2. 只读取 V2-C 已提交的主模型 token；每个 token 必须能追溯到永久原音对象和时间坐标。
3. 相邻 token 间隔不超过 45 秒且事件总跨度不超过 120 秒时归为同一证据事件。120 秒上限保证网页可以直接回听。
4. 每个事件保存原文、说话人 token 计数、无归属/不确定/重叠 token 数、ASR 分歧 ID 和原音引用。
5. provider 返回日级摘要、事件、事实和行动候选。V2-E.0 mock 只允许日级证据说明与逐事件占位卡，事实和行动必须为空。
6. 请求、响应、候选分别计算 SHA-256；数据库触发器禁止更新或删除。人工确认、驳回和文字修订写入独立追加表。

schema v10 新增：

- `semantic_exchanges`：一次 run 的不可变请求/响应、provider、模型、哈希和隐私声明。
- `semantic_candidates`：不可变日级/事件/事实/行动候选及证据。
- `semantic_candidate_revisions`：人工确认或驳回后的追加修订历史。

## 使用

```powershell
# 默认使用最新匹配的 V2-C/V2-D 完成 run
allday-asr semantic-v2 build 1

# 固定输入 run，或调整事件组织参数
allday-asr semantic-v2 build 1 --asr-run 10 --diarization-run 11 `
  --event-gap-seconds 45 --max-event-seconds 120

# 查看最近证据包
allday-asr semantic-v2 status 1

# 在本地网页生成、回听和审核
allday-asr web
```

网页的“语义证据”页会展示输入 run、provider、事件/token 数和隐私边界。事件卡可回听对应原音；修改标题或正文后选择“确认”或“驳回”，不会改变基础候选。

## 真实长录音验证

录音 1 的首次真实结果为 run 18：

| 项目 | 结果 |
| --- | ---: |
| 输入 | V2-C run 10 + V2-D run 11 |
| committed token | 2,192 |
| 证据事件 | 35 |
| 候选 | 36（1 个日级 + 35 个事件） |
| facts / actions | 0 / 0 |
| 网络访问 | 否 |
| 音频字节 / 路径进入请求 | 否 / 否 |
| 原 M4A | 79,826,205 字节，SHA-256 不变 |

请求 SHA-256 为 `cd67771737d055f480ca83aecc90115e5323394b6377e3ee1f4c7a748f8651ba`，响应 SHA-256 为 `c0cb5313569df1229c0659bbea5a2b05bde68f8165841dd573bc013d5000b7d7`。私有完整清单写入 `outputs/session-000001/semantic-v2e0/run-000018.json`，该目录不提交 Git。

## V2-E.1 的进入条件

下一阶段可以增加真实云端 provider，但必须继续遵守同一边界：

1. 明确配置供应商、模型、数据保留策略和上传字段，首次调用前单独授权。
2. 默认只上传最小化的文字证据；原音上传必须是独立、显式选择，不能由 provider 隐式读取本地路径。
3. 响应必须引用事件/token 证据；无证据的事实或行动候选不能进入可确认队列。
4. 保存原始云端响应和模型版本，不覆盖 ASR、说话人时间轴或既有语义 run。
5. 用人工审核集评测事实一致性、遗漏、幻觉和行动项精度后，再接入 Watch 同步后的日常批处理。
