# Computer 端超阈值模块维护完成记录

> 完成日期：2026-09-02
>
> 范围：Computer 仓库的 Python 后端与 V3 Desktop 前端；未修改 Harmony Phone/Watch。
>
> 门禁：生产 Python、TypeScript、TSX 与 Vue 源文件必须少于 500 行。

## 1. 完成结论

重构前共有 20 个生产源码文件达到或超过项目的 500 行维护阈值；A–E 五个工作包完成后为 0 个。拆分以业务职责和依赖方向为边界，原 HTTP/CLI、SQLite schema、模型参数、处理阶段顺序、证据链和前端导入入口保持不变。

原模块继续作为短小的显式 re-export 或 composition facade，已有调用方不需要一次性迁移。新增架构测试把 500 行清单升级为严格 ratchet：后续任何 Computer 端生产源码达到 500 行都会使测试失败。

## 2. A–E 工作包结果

### A：接口层

- Desktop server 拆为 HTTP contract、GET routes 与 POST routes。
- Transfer server 拆为 composition、HTTP handler、network、protocol 与 runtime。
- Passkey 拆为类型/编码、credential store 与 ceremony manager。

### B：人物与声纹

- People application 按 identity、prototype、cluster 与稳定 support 拆分。
- People SQLite repository 按 person、cluster/membership、prototype/review、identity policy/match decision 拆分。
- Person memory application 与 repository 独立成命令、刷新、查询和 codec 模块。

### C：知识、提醒与洞察

- Knowledge 拆为 generation、query/current state、resolution 与 derivation support。
- Reminder 拆为 command、query、apply state machine 与 projection/validation support。
- Insight 拆为 input snapshot、generation、management、evidence validation 与 projection。
- Knowledge SQLite 实现按 generation、event、memory、derivation 和 codec 拆分。

### D：持久处理与原生模型

- Durable processing 拆为 admission、job/run service、correction/invalidation、worker/lease 与 projection support。
- Native pipeline 拆为 contracts、orchestrator、audio preparation、artifact/utterance projection、factory 与 runtime profile。
- ASR backend 拆为 Qwen、FunASR、speech gate、model cache 与共享结果类型。
- Processing SQLite repository 拆为 admission、evidence projection、durable query、execution/lease、lifecycle 与 codec。

模型 ID、speech gate 阈值、窗口/padding、stage 顺序和复用指纹均未调整。

### E：仓储、领域解析与 Desktop 类型

- Repository ports 按 recording、processing、sync/desktop、knowledge/reminder、people/insight 拆分；`ports/repositories.py` 保留显式 re-export。
- 核心 SQLite repositories 按 recording、artifact、sync、operational 与稳定 row codec 拆分。
- Desktop read repository 按 recording、review/processing 和 system 查询模型拆分。
- Timeline quality 拆为 types/policy、document parser 与 evaluator/metrics。
- Desktop `core/types.ts` 拆为 recording、processing、desktop、reminder、people 与 insight 类型模块，并保留原入口 re-export。

## 3. 验证与持续门禁

- Python：完整 pytest 与 Ruff。
- Desktop：前端单元测试、`vue-tsc` 与 Vite production build。
- 架构：`test_computer_production_sources_stay_below_maintenance_threshold` 同时覆盖 `src/allday_asr/v3/**/*.py` 和 `all_day_recording_front/v3` 下的 `.ts`、`.tsx`、`.vue`。
- 构建只验证 Computer 端；未读取或修改手机端工程。

行数是触发器，不替代职责评审。后续若单个模块同时承载多个变化原因，即使尚未达到 500 行，也应优先按垂直切片继续拆分，避免重新形成跨 aggregate 巨型 facade。
