# Speaker Identity Blind Validation

日期：2026-09-28。**结论 B：继续收集 blind 数据。** 当前没有冻结后新增录音，状态为 `NO INDEPENDENT BLIND ACCEPTANCE DATA YET`。本轮只新增离线验证工具、结果和报告，未修改生产识别、阈值、自动确认、UI、同步或 ASR。

> 此仓库公开。具体人物、原音标识、时间范围、人工 fact、分数明细和复听片段仅保存在受 `.gitignore` 保护的本地 `outputs/speaker-identity-blind-validation-20260928/`；完整本地报告为其中的 `private-report.md`。本文件只提供去标识的可公开结论。

## 冻结方案

工具直接校验上一轮 Benchmark V2 的 manifest、冻结设置、结果及向量哈希，复用 CAM++、原会话均值 profile 与最高会话 cosine 分数：

| 方案 | 阈值 | Margin | V2 受控测试 |
| --- | --- | --- | --- |
| G：全局校准 | 0.420 | 0.100 | 已知 10/16、错人 0、库外误接收 0/21 |
| P：保守分人物校准 | 三个已入库人物分别使用 V2 冻结的 0.420、0.478、0.490；回退 0.420 | 0.100 | 已知 8/16、库外误接收 0/21 |

两者均要求代表语音累计至少 6 秒。本轮未改变 enrollment、representation、normalization、scoring、阈值或 margin。身份与阈值的对应关系只写入私有 `frozen-candidates.json`。

## 真正 blind 数据：尚无

V2 本地验证产物的冻结时点是 2026-09-28 11:48:59 UTC。盲测准入同时检查录制时间、session 建立时间、prototype 建立时间、原音资产建立时间均晚于该时点；还排除旧 session、prototype 和原音 SHA。数据库当前 annotation revision 与 V2 相同。扫描结果：新增 session **0**、符合条件的 prototype **0**、可评分 blind independent audio event **0**。

真值仅来自完整覆盖目标音频的有效人工人物事实；冲突或缺失标签不评分。自动人物事实和 G/P 预测均不作为真值。当前缺跨日期、跨设备、不同环境的已入库人物及库外 hard negative，也缺新的陌生人。`blind-manifest.json` 会在以后运行时发现新 session 并按同一冻结规则筛选，未满足真值条件的样本进入 `needs-human-review.json`。**旧测试数据没有被计入正式 blind 指标。**

## 独立原音事件：仅历史诊断

通过原音 SHA 与时间重叠、同一 track/cluster 的会话时间邻近，先将相关 prototype 合并，再评分。上一轮 37 条真实自动轨 prototype 中，24 条有完整单人事实；另有无标签 10、不完整 2、多人物标签 1，已生成待人工复核清单。24 条可评分 prototype 合并为 **11 个历史独立事件**，覆盖 4 个 session、3 个日期，其中已入库 4 个、库外 7 个。合并和结果见本地私有 `independent-events.json` 与 `event-level-results.json`。

| 历史 independent event 指标 | G | P |
| --- | ---: | ---: |
| 已知认对 | 3/4 | 3/4 |
| 已知拒识 | 1/4 | 1/4 |
| 已知认错 | 0 | 0 |
| 库外正确拒识 | 6/7 | 7/7 |
| 库外误接收 | 1/7 | 0/7 |
| 本人 → 其他人物 | 1 个事件 | 0 个事件 |

同一高风险事件有 10 个重叠 prototype，G 的 3 次 prototype 级误接收在主要统计中只算 **1 个错误事件**。P 均拒识。原音定位和 5 段可复听短音频已放在私有输出目录；人工事实完整覆盖代表窗口，但尚未独立听音，混音、重叠、边界纯净度和 track 污染仍待核对。Prototype 级数字仅供诊断，不是 24 次独立证据。

## 三层分区与时间线

仅用冻结 G/P 门槛离线分区：低于 G 为 Unknown，过 G 未过 P 为 suggestion，过 P 为 auto-commit candidate。11 个历史事件中，已知 suggestion **0**、库外 suggestion **1**、错误进入 auto-commit candidate **0**。这只是分数区间分析，没有实现 UI 或自动确认；没有 blind 数据可支持产品决定。

同款 CAM++ 重提取了 24 条已标注自动轨的 65 个不同代表 clip，按会话时间输出累计语音分数、候选差距和 G/P 状态。部分同一事件的前缀状态在累积语音后又返回 Unknown；4 条短轨的重提取最终向量与数据库向量 cosine 低于 0.99。代表 clip 多数本身约 8 秒且可相隔较远，不能测得精确 1/3/5 秒的连续流决策，也不能把首次过门槛当永久提交。结论为 `insufficient evidence for temporal commitment`。

## Acceptance Gate 与复现

**生产决策：继续收集 blind 数据。** P 在旧本人事件上减少误认，但正式 blind event 为 0，现有证据不足以进入生产设计，也不足以据新数据判定两个候选失败。

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe tools\validate_speaker_identity_blind.py
```

脚本使用 SQLite `mode=ro` 和 `query_only`，核对模型及冻结来源哈希。私有输出含 `PROTOCOL.md`、`frozen-candidates.json`、`data-leakage-audit.json`、`independent-events.json`、`blind-manifest.json`、`needs-human-review.json`、`event-level-results.json`、`prototype-level-diagnostics.json`、`timeline-results.json`、`failure-cases.json` 和 `verification.json`。公开 Git 仅保留工具与本去标识报告。
