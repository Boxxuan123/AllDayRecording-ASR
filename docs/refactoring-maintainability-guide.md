# AllDayRecording-ASR 可维护性重构指南

> 状态：执行中；阶段 0、阶段 1、阶段 2A、阶段 2B、阶段 2C、阶段 3 已于 2026-08-30 完成
> 编写日期：2026-08-30  
> 适用范围：`src/allday_asr/`、`tests/` 与相关工程文档  
> 当前回归基线：116 个单元测试通过

## 1. 目的与结论

项目已经形成可运行、可追溯、可评测的完整流程。当前主要风险不是功能错误，而是功能增长集中在少数文件中，导致模块边界逐渐失守：数据库、CLI、Web、工作流和版本化服务同时承担过多职责。

本轮重构的目标是：

1. 保持现有业务结果、数据库兼容性和命令/API 行为不变。
2. 让模块名表达业务职责，而不是只表达开发阶段或版本号。
3. 固定单向依赖，避免服务之间横向调用和循环导入。
4. 将数据库、模型、文件系统和 HTTP 等基础设施与业务规则分开。
5. 让一次普通功能修改只影响一个明确区域，而不是同时修改多个超大文件。

这不是一次重写。所有调整都应采用“小步提取、兼容转发、测试确认、再删除旧入口”的方式完成。

## 2. 当前结构评估

当前目录已有合理雏形：

- `audio/` 主要封装 FFmpeg、探测、标准化和音频裁剪。
- `asr/` 与 `diarization/` 主要封装模型后端。
- `services/` 承担用例和处理流程。
- `storage/` 封装 SQLite。
- `cli.py`、`web.py` 和 `web_assets/` 提供用户接口。
- `tests/` 按主要能力建立了回归测试。

主要结构债务如下：

| 区域 | 当前现象 | 维护风险 |
| --- | --- | --- |
| `storage/database.py` | schema、15 个版本迁移、连接管理、全部仓储、部分业务校验集中在一个文件 | 任意数据能力都修改同一文件；接口过大；构造时存在隐藏 I/O |
| `cli.py` | 所有 Typer 子命令、模型工厂、配置转换和输出格式集中 | 新增命令容易继续扩大入口文件；CLI 与运行时装配耦合 |
| `web.py` | 查询聚合、业务写操作、后台 job、音频裁剪、HTTP 路由集中 | 领域逻辑与传输协议难以独立测试；路由分支持续增长 |
| `web_assets/app.js` | 全局状态、API、播放器、时间轴、身份/语义/评测界面集中 | 修改一个页面容易影响其他页面；缺少可独立测试的前端模块 |
| `quality_workflow.py` | 一个主函数编排准入、阶段复用、执行、降级、审核和持久化 | 成功/失败语义难以局部理解；新增阶段继续增加分支 |
| `semantic_v2e0*`、`quality_diarization_v2d*` | 版本号文件相互导入并复制部分结构 | 当前入口和历史兼容代码难区分；版本号替代了职责边界 |
| 服务横向依赖 | ASR 引用 benchmark/evaluation，timeline 引用 exporter 私有函数，backup/readiness 形成循环 | 依赖方向不稳定；一个服务的内部重构会破坏其他服务 |
| `dict[str, Any]` 与 `sqlite3.Row` | 大量跨模块返回值依赖字符串键 | 重命名和字段遗漏只能在运行时发现 |
| `paths.py` | 项目根目录、输出目录和模型缓存是模块级全局状态 | 打包、测试隔离和多实例运行困难；目录创建和环境变量修改存在隐式副作用 |

文件长度只是信号，不是拆分目标。真正需要解决的是职责数量、变化原因和依赖方向。

## 3. 重构期间不可破坏的约束

以下约束高于代码整洁度。任何重构都必须证明它们仍然成立。

### 3.1 数据与证据不变量

1. 原始 Watch 音频永久保存，绝不原地修改或由派生产物覆盖。
2. 原音 SHA-256、字节数、时间坐标和 source trace 的含义不得改变。
3. 已完成 processing run、模型输出、真值集和基础语义候选保持不可变。
4. schema 变化只能追加新 migration，不得修改已经发布的历史 migration。
5. 迁移前自动备份、失败回滚和高版本拒绝逻辑必须保留。
6. 历史 run 必须仍可按其 `pipeline_version` 读取、导出或重放。

### 3.2 对外兼容性

1. 已有 CLI 命令名、参数名、退出码和关键输出含义默认保持不变。
2. 已有 HTTP 路径、方法、认证方式、状态码和 JSON 字段默认保持不变。
3. `allday-asr` 的安装入口保持不变。
4. 若确实需要不兼容修改，必须先增加兼容层和弃用说明，不得在纯重构提交中直接删除。

### 3.3 模型与流程语义

1. 重构不得隐式改变模型 ID、精度、阈值、窗口、padding 或阶段复用规则。
2. V2-C、V2-D、V2-D.1/D.2/D.3 和 V2-E 的失败/降级边界保持不变。
3. 本地/云端数据边界保持不变；不得因重构扩大上传内容。
4. 同一配置、输入指纹和模型签名应产生相同的复用决定。

## 4. 目标依赖方向

目标不是机械地建立很多目录，而是形成以下单向依赖：

```text
CLI / Web / 其他用户接口
            │
            ▼
Application：用例、工作流、事务边界、DTO
            │
            ▼
Domain：纯规则、值对象、区间算法、审核政策
            │
            ▼
Ports：Repository、模型后端、文件存储等协议
            ▲
            │ implements
Infrastructure：SQLite、FFmpeg、模型实现、文件系统
```

必须遵守的规则：

- Domain 不导入 SQLite、HTTP、Typer、具体模型或项目路径。
- Application 可以组合多个领域能力，但只依赖协议或窄接口。
- Infrastructure 实现协议，不反向调用 CLI、Web 或完整工作流。
- CLI/Web 只负责参数解析、认证、请求/响应转换和展示。
- 普通 service 不导入另一个 service 的私有函数。
- 只有明确的 workflow/use case 可以编排多个业务服务。
- 为绕过循环导入而使用函数内 import，应视为需要消除的结构问题。

一个可逐步靠近的目录形态如下：

```text
src/allday_asr/
  config.py
  paths.py                    # 过渡期保留，最终由 AppPaths 取代全局状态
  domain/
    intervals.py
    text.py
    identity.py
    workflow.py
    semantic.py
  application/
    dto/
    workflows/
      quality.py
      daily.py
    use_cases/
      sessions.py
      speaker_review.py
      semantic_review.py
  ports/
    repositories.py
    asr.py
    diarization.py
    storage.py
  infrastructure/
    sqlite/
      connection.py
      migrations/
      repositories/
    audio/
    asr/
    diarization/
  interfaces/
    cli/
    web/
  web_assets/
    api.js
    state.js
    audio.js
    views/
```

该目录是方向说明，不要求一次完成。重构开始时应先提取明确职责，再决定是否移动整个目录。

## 5. 分阶段实施方案

### 阶段 0：建立可重复的重构基线

目标：在移动代码前，明确什么行为不能变化。

实施记录见 [可维护性重构阶段 0 基线](refactoring-phase-0-baseline.md)：已记录
全量测试命令与结果，增加 CLI smoke、Web 端点清单、workflow 策略矩阵、
Ruff 项目规则和防止新增依赖债务的架构测试。本阶段未修改生产代码行为。

实施内容：

1. 记录当前完整测试命令和结果。
2. 为关键 CLI 建立最小 smoke test：帮助信息、参数错误、成功退出和失败退出。
3. 为 Web 建立端点清单，包括方法、路径、认证要求、状态码和响应顶层字段。
4. 为 workflow 建立阶段复用和失败策略矩阵。
5. 在 `pyproject.toml` 中配置 Ruff，而不是直接接受默认全部规则。
6. Typer 参数默认值需要按框架惯例配置 `B008` 例外，避免无意义噪声。
7. 将 import cycle、跨 service 私有导入和新增超大函数作为审查项。

验收条件：

- 现有 79 个测试继续通过。
- smoke test 不加载真实模型、不读取真实音频。
- lint 对新增代码形成干净基线；历史问题可分批清理。
- 本阶段不改变生产代码行为。

### 阶段 1：先清理共享纯函数和循环依赖

目标：建立最小、稳定、无副作用的公共层，为后续拆分创造接缝。

实施记录见 [阶段 1：共享纯函数与依赖接缝](refactoring-phase-1-shared-primitives.md)：
文本、区间、时间、embedding、canonical JSON hash 和 session integrity 已提取；
旧入口保留兼容 re-export，阶段 0 记录的私有导入、循环依赖和函数内 service
import 白名单均已清零。

优先迁移：

| 当前能力 | 当前依赖方向 | 建议目标 |
| --- | --- | --- |
| `normalize_text` | `quality_asr → benchmark` | `domain/text.py` |
| `levenshtein_operations` | `quality_asr → evaluation` | `domain/text.py` 或 `domain/metrics.py` |
| `_group_segments`、绝对时间格式化 | `timeline → exporters` 私有函数 | `domain/intervals.py`、`domain/time.py` |
| `l2_normalize`、`normalize_speech_level` | identity/verification → enrollment | `audio/embeddings.py` 或 `domain/voice.py` |
| session 输入校验 | backup 与 readiness 互相引用 | `application/use_cases/session_integrity.py` |
| JSON canonical hash | 多个 service 各自实现 | 明确语义后提取到 `domain/hashing.py`；不同协议不得强行合并 |

迁移方式：

1. 先给纯函数增加直接单元测试。
2. 将实现移动到目标模块。
3. 在旧模块保留临时 re-export，避免一次修改所有调用方。
4. 分批改为从新模块导入。
5. 确认没有外部调用后删除兼容 re-export。

验收条件：

- `services/` 不再导入其他 service 的私有名称。
- `session_backup` 与 `session_readiness` 不再循环依赖。
- 共享模块不依赖数据库、路径或模型实例。
- 全量测试通过，文本规范化、编辑距离和时间区间结果与原实现一致。

### 阶段 2：拆分 SQLite 基础设施

目标：把连接、迁移和各业务仓储分开，同时保持现有 `Database` 调用接口。

建议分三步完成。

#### 2A：提取连接和 migration

实施记录见 [阶段 2A：SQLite 连接与迁移基础设施](refactoring-phase-2a-sqlite-migrations.md)：
连接事务、migration runner 和 v001–v015 历史 SQL 已提取；组合 SQL 哈希保持不变，
空库 schema 快照、全部历史版本升级、升级前备份、失败回滚和高版本拒绝均已有测试。
`Database` 保持兼容门面；阶段 2B repository 拆分随后已完成。

```text
infrastructure/sqlite/
  connection.py
  migration_runner.py
  migrations/
    v001_initial.py
    ...
    v015_manual_identity.py
```

- 已发布 SQL 内容原样迁移，不做“顺便整理”。
- `LATEST_SCHEMA_VERSION` 和执行顺序只由 migration runner 管理。
- schema guard、backfill 和迁移前备份要有明确测试。
- 新库初始化和旧库升级使用同一个 runner。

#### 2B：按业务区域提取 repository

实施记录见 [阶段 2B：核心业务 Repository](refactoring-phase-2b-repository-seams.md)：
Session、Run、ASR、Diarization、Identity、Semantic、Evaluation 和 Action 八个核心
repository 与 source fingerprint 已提取，96 个原 `Database` 方法保留显式兼容转发；
真实 SQLite 测试覆盖直接调用、门面一致性及关键事务回滚。下一步进入阶段 2C，显式化
数据库初始化和 migration I/O。

建议至少分为：

- `SessionRepository`
- `RunRepository`
- `AsrRepository`
- `DiarizationRepository`
- `IdentityRepository`
- `SemanticRepository`
- `EvaluationRepository`
- `ActionRepository`

过渡期继续保留：

```python
class Database:
    """兼容门面；新代码应依赖窄 repository。"""
```

旧调用方仍可使用 `database.list_session_processing_runs(...)`，门面内部委托给对应 repository。新代码不再向门面添加新业务方法。

#### 2C：移除构造函数隐藏副作用

实施记录见 [阶段 2C：显式数据库生命周期](refactoring-phase-2c-explicit-database-lifecycle.md)：
`Database.__init__` 现在只解析路径并装配 repository；目录创建和 migration 由
`initialize()` 显式执行，普通入口统一使用 `Database.open(...)`。CLI、Web 和测试中的
86 个调用点已完成迁移，构造无 I/O、重复打开幂等及历史 migration 行为均有自动测试。

目标接口应区分：

- 创建对象；
- 显式初始化/迁移；
- 打开一次事务；
- 取得 repository。

可先增加 `Database.open(...)` 或应用启动工厂，再逐步迁移调用方。只有所有入口完成迁移后，才调整 `Database.__init__` 的旧行为。

数据库层应保留数据完整性校验，但业务政策应上移。例如“外键和时间范围合法”属于持久化边界；“某类语义 run 是否允许生成某类候选”更适合放在 application/domain。

验收条件：

- 从空库初始化到最新 schema 的表、索引、trigger 与当前一致。
- 支持的每个历史 schema 都可升级，且升级前备份仍会生成。
- 迁移失败时事务回滚，不写入对应 migration 版本。
- repository 有事务级测试。
- `Database` 兼容门面的公开行为不变。
- `database.py` 不再包含全部 migration SQL 和全部业务仓储实现。

### 阶段 3：拆分质量工作流

实施记录见 [阶段 3：显式质量工作流阶段](refactoring-phase-3-quality-workflow.md)：
`WorkflowContext`、`StageResult`、`ReviewReason` 和七个具体 runner 已提取到
`application/workflows/`；原 749 行 service 只保留 17 行兼容导出。主入口和阶段编排
分别收敛到 81/83 行，复用、续跑、非阻断增强及 review JSON 均有直接测试。

目标：让每个阶段的输入、输出、复用和失败语义可以独立理解与测试。

建议引入少量显式类型：

```python
@dataclass(frozen=True)
class WorkflowContext:
    session_id: int
    recording_id: int | None
    input_fingerprint: str
    admission_mode: str

@dataclass(frozen=True)
class StageResult:
    stage: str
    run_id: int | None
    status: str
    reused: bool
    summary: Mapping[str, object]

@dataclass(frozen=True)
class ReviewReason:
    code: str
    stage: str
    message: str
    details: Mapping[str, object]
```

主工作流只保留以下骨架：

```text
检查准入
  → resolve_or_run_asr
  → resolve_or_run_diarization
  → run_optional_speech_recall
  → run_optional_identity_audit
  → inspect_identity_mining_state
  → resolve_or_run_semantic
  → 汇总审核原因
  → 完成 workflow run
```

每个 stage runner 必须明确：

- 输入设置和依赖的上游 run；
- 复用匹配条件；
- 新建 run 的时机；
- 成功摘要；
- 失败是否终止基础流程；
- 是否产生人工审核原因。

不要引入通用工作流框架。当前阶段数量有限，具体、可读的 runner 比抽象 DAG 引擎更适合。

验收条件：

- 主工作流函数主要表达顺序，不再内嵌各阶段查询细节。
- 复用匹配逻辑有参数化测试。
- D.1/D.2 增强失败仍不会错误覆盖已经完成的基础结果。
- workflow 状态和 review reason 的 JSON 兼容现有 Web/CLI。
- 相同输入产生与重构前一致的 run 复用决策。

### 阶段 4：拆分 Web 后端和前端

目标：传输层只做协议工作，业务操作由 application use case 完成。

#### 后端

阶段 4B 已于 2026-08-30 完成，实际结构为：

```text
interfaces/web/
  application.py
  server.py
  router.py
  auth.py
  responses.py
  params.py
  jobs.py
  audio.py
  presenters.py
  routes/
    assets.py
    workspace.py
    timeline.py
    semantic.py
    evaluation.py
    actions.py
    audio.py
  use_cases/
    workspace.py
    evaluation.py
    timeline.py
    semantic.py
    background.py
    media.py
```

- route 只读取参数、调用 use case 并返回稳定 JSON；server 统一映射异常。
- presenter 把数据库行和 DTO 转成稳定 API payload。
- job registry 只保存线程安全的状态和进度，不承担业务计算。
- 音频 Range、普通文件和 JSON 响应已分开；主工作台与盲标台复用安全接缝。
- `allday_asr.web` 保留原有公开入口，HTTP server 框架和 URL 不变。

#### 前端

阶段 4A 已于 2026-08-30 使用项目内已有的 Vue 3/Vite 骨架完成。源码结构为：

```text
all_day_recording_front/src/
  api/client.js
  state/workspace.js
  audio/playback.js
  utils/format.js
  workspace/
    controller.js
    dom.js
    reload.js
  components/
    layout/
    views/
  views/
    timeline.js
    semantic.js
    evaluation.js
    actions.js
    runs.js
    dashboard.js
    sessions.js
```

`controller.js` 只负责初始化、导航、高层事件连接和会话聚合；SFC 负责稳定页面区域，
各 view 模块负责自己的动态内容。Vite 使用稳定产物名生成到
`src/allday_asr/web_assets/`，构建产物提交并随 Python 包发布，因此最终用户运行
`allday-asr web` 仍不依赖 Node。旧盲标页面也移入前端工程的 `public/`，由同一次
构建发布。

阶段 4A 的详细迁移记录见
[Vue 前端迁移记录](refactoring-phase-4a-vue-frontend.md)，阶段 4B 见
[Web 后端模块化记录](refactoring-phase-4b-web-backend.md)。阶段 4 已整体完成，
下一步进入阶段 5。

验收条件：

- 原有 URL、HTTP 方法、认证和 JSON 字段保持兼容。
- 每个路由模块可独立测试成功、参数错误、未授权和不存在资源。
- 后台 job 的 running/completed/failed 状态有测试。
- 音频 Range 请求继续返回正确的 `206` 和边界。
- 浏览器仍可在无 Node 构建流程的情况下直接打开工作台。
- 前端至少为纯格式化、区间计算和状态转换函数增加测试。

以上条件均已通过：Python 全量 123 个测试、前端 4 个测试、Vite 生产构建和浏览器
五视图冒烟均成功，控制台无 warning/error。

### 阶段 5：收拢版本化 semantic 与 diarization 代码

目标：保留历史协议和 run 可重放能力，但让当前代码按职责组织。

建议原则：

1. `pipeline_version`、请求格式和证据格式继续保存在数据中。
2. 当前生产入口使用稳定名称，例如 `semantic.pipeline.run(...)`。
3. 历史实现放入 `legacy/`，由版本 dispatcher 明确选择。
4. 多版本共享的 token 读取、证据构建、响应校验和 overview 分别提取。
5. 禁止最新版通过导入旧版主流程来获得公共能力；公共能力应拥有独立模块。

示例：

```text
application/semantic/
  pipeline.py
  evidence.py
  transport.py
  validation.py
  overview.py
  versions.py
  legacy/
    v2e0.py
    v2e01.py
```

diarization 可按职责拆为：

- 基础说话人时间轴；
- speech recall；
- identity contamination audit；
- identity candidate mining；
- 人工审核和 truth 构建。

验收条件：

- 给定历史 run/fixture，旧版本 overview 和导出结果保持一致。
- 当前入口只有一个明确的默认版本。
- 新版模块不再从旧版模块导入公共实现。
- 版本 dispatcher 对未知版本明确失败，不静默回退。

### 阶段 6：拆分 CLI 和运行时装配

目标：根 CLI 只注册命令，模型工厂和应用装配集中在 composition root。

建议结构：

```text
interfaces/cli/
  app.py
  runtime.py
  output.py
  commands/
    sessions.py
    workflow.py
    asr.py
    diarization.py
    semantic.py
    benchmark.py
    library.py
    legacy.py
```

- `app.py` 只创建 Typer app 和注册子 app。
- `runtime.py` 将配置转换为 backend、settings 和 repository。
- command 只负责参数解析、调用 use case、显示结果和转换退出码。
- 重复的 `--db`、`--config` 与 target 解析可形成小型公共依赖，但不要隐藏用户输入。

验收条件：

- 所有现有命令名、选项和 help 文本仍可用。
- 模型工厂可在测试中替换，不需要导入或加载真实模型。
- 单个命令模块不需要导入全部业务服务。
- 根 `cli.py` 变为兼容入口或薄转发文件。

### 阶段 7：显式路径、配置与跨层 DTO

目标：减少模块级全局状态和字符串键协议。

建议增加：

```python
@dataclass(frozen=True)
class AppPaths:
    state_dir: Path
    output_dir: Path
    model_dir: Path
    config_path: Path
    database_path: Path
```

应用启动时从环境变量和配置构造一次 `AppPaths`，再注入需要访问文件系统的组件。纯 domain/application 规则不应自行读取全局路径。

DTO 优先用于以下边界：

- workflow stage 结果；
- Web route 与 presenter 之间；
- repository 返回给 application 的核心实体；
- semantic provider 请求/响应；
- identity review 和 timeline candidate。

不需要一次性引入完整 ORM。SQLite repository 内仍可使用 `sqlite3.Row`，但跨出 repository 时应逐步转换为 dataclass、TypedDict 或专用只读对象。

验收条件：

- 测试可以用临时 `AppPaths` 隔离状态，不依赖 import 顺序。
- 创建服务对象不会隐式修改模型缓存环境变量。
- 关键跨模块结构具备静态字段检查。
- JSON 序列化边界集中在 presenter/transport 层。

## 6. 测试策略

重构遵循“先刻画、后移动、再收紧”的测试顺序。

### 6.1 Characterization test

在移动一段缺乏直接测试的代码前，先记录其当前行为。重点包括：

- migration 从各历史版本升级后的对象结构；
- workflow 阶段复用、增强失败和空 token 行为；
- Web API 顶层字段、状态码和错误文本类别；
- semantic 历史版本的证据哈希、候选顺序和 review clip；
- CLI 退出码和关键输出。

### 6.2 Contract test

为协议建立可复用测试：

- 所有 repository 的 get/list/not-found/transaction 行为；
- ASR、diarization、semantic provider 的 fake 实现；
- Web route 对 use case 异常的映射；
- pipeline version dispatcher。

### 6.3 Golden fixture

以下输出适合保存小型、脱敏、人工可审查的 golden fixture：

- semantic evidence ledger 和 provider payload；
- workflow summary；
- timeline overview；
- migration 后 schema 元数据。

Golden fixture 只保存必要的小型结构，不复制真实音频、隐私内容或整库。

### 6.4 每次提交的最低验证

1. 运行受影响模块的测试。
2. 运行完整 79+ 测试套件。
3. 运行配置后的 Ruff。
4. 若涉及 CLI，运行对应 `--help` 和错误路径 smoke test。
5. 若涉及 Web，运行认证、API、音频 Range 和 job 测试。
6. 若涉及 migration，至少测试空库、最近旧版和一个更早版本升级。

不得以“只是移动文件”为理由跳过验证；import、初始化顺序和资源路径都可能在移动时改变。

## 7. 提交与评审规则

每个重构提交只解决一个结构问题，并满足以下要求：

- 尽量不同时修改算法和目录结构。
- 若必须修改行为，先独立提交行为测试和修复，再做移动。
- 提交说明写明旧入口、新入口、兼容层和删除条件。
- 不顺便格式化无关大文件，避免掩盖语义变化。
- 不覆盖工作区中与当前重构无关的用户改动。
- 删除兼容层前先全仓搜索调用方，并在前一阶段标记弃用。
- 每个阶段完成后更新本指南和 `docs/project-status.md`，不要让状态文档继续描述已不存在的结构。

建议每个工作包按以下顺序提交：

```text
characterization tests
  → 新模块与兼容转发
  → 调用方迁移
  → 架构/依赖测试
  → 删除无调用旧实现
  → 文档更新
```

## 8. 不应采用的做法

1. 不一次性移动整个 `src/allday_asr`。
2. 不在同一提交中更换 ORM、Web 框架、CLI 框架和目录结构。
3. 不仅按文件行数机械拆文件；拆分必须对应独立变化原因。
4. 不为了消除重复而合并语义不同的哈希、时间范围或验证规则。
5. 不使用大而全的 `utils.py`、`common.py` 重新制造新的混杂模块。
6. 不把 `Database` 拆成几十个只有一条 SQL 的无语义类。
7. 不删除历史 pipeline 实现，直到历史 run 的读取和重放已有明确替代方案。
8. 不用 mock 掩盖 migration、事务、文件完整性等必须真实验证的边界。
9. 不把 lint 自动修复与结构重构混为同一批不可审查的变化。

## 9. 完成定义

当以下条件全部满足时，可认为本轮可维护性重构完成：

- 数据库迁移、连接和业务 repository 已分离。
- 新业务代码依赖窄 repository，不再扩展巨型 `Database` 门面。
- CLI 和 Web 入口只承担协议、装配和展示职责。
- 质量工作流由可独立测试的阶段组成，主流程清楚表达执行顺序。
- semantic/diarization 当前入口明确，历史版本集中管理。
- `services/` 之间没有私有函数导入或循环依赖。
- 关键跨层结构不再依赖未声明的 `dict[str, Any]`/`sqlite3.Row` 字段。
- 路径和模型缓存配置可由测试显式注入。
- Python lint 对约定范围保持干净，新增依赖循环能被自动发现。
- CLI/API 兼容测试、migration 测试和完整单元测试持续通过。
- 原始音频、run、truth、source trace 和历史证据的不变量全部保留。

## 10. 推荐的前三个工作包

### 工作包 A：低风险依赖整理

- 提取文本、区间、时间和 embedding 纯函数。
- 消除 backup/readiness 循环依赖。
- 禁止跨 service 私有函数导入。
- 建立 Ruff 的项目规则和新代码基线。

该工作包改动风险最低，同时为后续拆分建立稳定公共层。

### 工作包 B：数据库迁移与 repository 接缝

- 先提取 migration runner 和历史 SQL。
- 再按两个批次提取八个核心业务 repository。
- 保留 `Database` 兼容门面。
- 增加空库、历史升级、回滚和门面兼容测试。

首批优先选择 Session/Run/Semantic，是因为它们正被 workflow、Web 和版本化 semantic
共同使用；第二批已完成 ASR/Diarization/Identity/Evaluation/Action。

### 工作包 C：Web 工作台模块化

- 后端先拆 presenter、job 和 timeline/identity/semantic route。
- 前端再拆 API、state、audio 和三个对应 view。
- 保持 URL 与 payload 不变。
- 补充独立路由测试和纯前端函数测试。

完成这三个工作包后，再决定 quality workflow、CLI 和历史版本模块的具体迁移顺序。这样可以先处理当前增长最快、冲突最频繁的区域，同时保持每一步都可回滚、可验证。
