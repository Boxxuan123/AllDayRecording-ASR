# Computer 端超阈值模块维护计划

> 盘点日期：2026-09-02  
> 范围：Computer 仓库的 Python 后端与 V3 Desktop 前端；不包含 Harmony Phone/Watch。  
> 判定：沿用项目已经声明的 500 行维护阈值。行数只作为评审触发器，拆分边界仍以职责、变化原因和依赖方向为准。

## 1. 当前盘点

当前共有 20 个源码文件超过 500 行：

| 职责组 | 文件 | 行数 |
| --- | --- | ---: |
| 人物与声纹 | `adapters/sqlite/people_repository.py` | 1818 |
| Desktop 接口 | `interfaces/desktop_server.py` | 1483 |
| 人物与声纹 | `application/people.py` | 1165 |
| 设备接收接口 | `interfaces/transfer/server.py` | 1092 |
| 洞察 | `application/insights.py` | 1078 |
| 持久处理仓储 | `adapters/sqlite/processing_repositories.py` | 997 |
| 核心仓储 | `adapters/sqlite/repositories.py` | 994 |
| 持久处理 | `application/durable_processing.py` | 871 |
| 知识 | `application/knowledge.py` | 848 |
| 知识仓储 | `adapters/sqlite/knowledge_repositories.py` | 835 |
| 原生模型管线 | `adapters/models/native.py` | 824 |
| 提醒 | `application/reminders.py` | 816 |
| 人物记忆仓储 | `adapters/sqlite/person_memory_repository.py` | 754 |
| ASR backend | `adapters/models/asr_backends.py` | 732 |
| Repository ports | `ports/repositories.py` | 643 |
| Passkey | `interfaces/transfer/passkeys.py` | 640 |
| Desktop 查询仓储 | `adapters/sqlite/desktop_repository.py` | 632 |
| 时间线质量 | `domain/timeline_quality.py` | 609 |
| Desktop 类型 | `all_day_recording_front/v3/core/types.ts` | 605 |
| 人物记忆 | `application/person_memory.py` | 591 |

最需要优先处理的不是所有超阈值文件，而是同时具有“文件大、职责多、修改频繁、跨层影响”特征的四组：接口层、人物与声纹、知识/洞察/提醒、持久处理与模型。

## 2. 推荐工作包

### 工作包 A：先拆两类接口层

1. 将 `desktop_server.py` 拆成 server/session 安全边界、静态资源、请求解析，以及 recordings、processing、people、knowledge/reminders/insights 等显式 route 模块。
2. 将 `transfer/server.py` 拆成上传协议 handler、V3 Device API handler、server composition、运行时启动输出和局域网地址发现。
3. 将 `passkeys.py` 拆成 credential store、ceremony manager、request binding/编码三个模块。

HTTP 路径、方法、认证、状态码、JSON 字段和启动提示先用 characterization test 固定。主 handler 只保留认证、解析和分派，不把业务规则迁回接口层。

### 工作包 B：人物与声纹形成垂直切片

1. 将 `application/people.py` 按人物生命周期与合并、声簇分析与匹配、prototype 审核、identity policy/历史重匹配拆分。
2. 将 `people_repository.py` 按 person、cluster/membership、prototype/review、identity policy/match decision 四个 aggregate repository 拆分。
3. 将 `person_memory.py` 与其 repository 作为独立切片，避免人物 CRUD、声纹算法和长期记忆继续共享同一变化面。

先在 `ports/` 建立窄协议，再迁 application 调用方；不要保留一个新的“大 PeopleRepository”门面继续增长。

### 工作包 C：拆开知识、提醒和洞察的生成与状态管理

1. `knowledge.py` 分为 generation/proposal、review/resolution、current state、derivation cascade。
2. `reminders.py` 分为候选生成与校验、审核状态机、schedule 投影和去重策略。
3. `insights.py` 分为输入快照、Codex 生成、证据校验/链接、daily report、relationship report。
4. SQLite 实现按相同 aggregate 边界拆分，不创建跨业务的 `utils.py`；仅把真正稳定的 evidence 引用和值对象放入 domain/contracts。

这组拆分必须保持 append-only revision、证据引用、stale 传播和撤回/恢复语义不变。

### 工作包 D：持久处理和原生模型管线

1. `durable_processing.py` 分为 admission、job/run service、correction/invalidation、worker/lease。
2. `native.py` 保留薄 pipeline orchestrator，将音频窗口准备、ASR/diarization stage、utterance projection 和 artifact 发布拆开。
3. `asr_backends.py` 按 Qwen、FunASR 和 speech gate 分模块；speech gate 保持纯函数和独立测试。
4. `processing_repositories.py` 按 durable job/run/lease、admission、evidence projection 拆分。

该工作包不同时改模型 ID、阈值、窗口、padding、stage 顺序或复用指纹。

### 工作包 E：收尾仓储、领域解析和前端类型

1. 将 `ports/repositories.py` 按 recording/processing/device/knowledge/people/insight 拆分，并通过包级显式 re-export 平滑迁移调用方。
2. 将 `repositories.py` 中已经独立的 11 个 SQLite repository 移到对应文件；共享 row codec 只保留确定稳定的序列化原语。
3. `desktop_repository.py` 按 Desktop 页面查询模型拆分，避免继续形成跨 aggregate 的读侧巨类。
4. `timeline_quality.py` 仅在有独立变化需求时拆为 document parser 与 evaluator/metrics；它当前虽超阈值，但领域内聚度高，优先级低于前四组。
5. `core/types.ts` 按 recordings、processing、people、reminders、insights feature 拆分，再由 `core/types.ts` 短期 re-export，避免一次性修改全部 view。

## 3. 实施和门禁

每个工作包遵循同一顺序：

```text
characterization/contract test
  → 新窄模块与临时 re-export
  → 分批迁移调用方
  → 架构依赖测试
  → 删除无调用兼容层
  → 文档与行数基线更新
```

建议先增加一个非阻断的 500 行清单，再升级为 ratchet：既有超阈值文件不能继续增长，新文件不得超过阈值；只有当某一工作包完成后，才把对应文件移入严格门禁。这样不会为了让 CI 变绿而做无语义的机械切割。

所有阶段都应保持 schema、migration 历史、HTTP/CLI 协议、模型行为和证据链不变，并运行受影响测试、完整 Python 测试、Ruff；涉及 Desktop 前端时再运行 TypeScript 测试与生产构建。
