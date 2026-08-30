# 可维护性重构阶段 2B：核心业务 Repository

> 完成日期：2026-08-30
> 前置基线：阶段 2A，98 个测试通过
> 交付基线：106 个测试通过
> 范围：提取八个核心业务 repository，保留 `Database` 兼容门面

## 1. 结果

核心 repository 位于：

```text
infrastructure/sqlite/repositories/
  sessions.py
  runs.py
  asr.py
  diarization.py
  identity.py
  semantic.py
  evaluation.py
  actions.py
```

`Database` 在构造时提供对应的窄入口：

```python
database.sessions
database.runs
database.asr
database.diarization
database.identity
database.semantic
database.evaluation
database.actions
```

既有 `database.get_recording_session(...)`、`create_asr_hypothesis(...)`、
`replace_speaker_labels(...)`、`create_truth_set(...)` 和
`review_action_candidate(...)` 等公开方法仍然存在，但只负责按原签名转发。
两个批次合计提取并转发 96 个公开方法，其中本批新增 73 个；CLI、Web 和既有 service
无需同时迁移。第二批完成后，`storage/database.py` 从约 3,565 行降到 1,867 行。

repository 只依赖连接工厂、时钟以及显式注入的窄依赖，不导入
`storage.database.Database`。主要依赖关系为：

```text
SessionRepository
  ├─> RunRepository
  │     ├─> AsrRepository
  │     ├─> DiarizationRepository
  │     ├─> IdentityRepository
  │     └─> SemanticRepository
  └─> EvaluationRepository

ActionRepository（独立）
```

source input fingerprint 已在首批从兼容门面提取到独立 SQLite 基础设施模块，Session
与 Run 继续使用同一实现，输出语义保持不变。

## 2. 事务与兼容性

- Run 创建及其 `processing_run_inputs` 仍在同一连接事务中；输入外键失败时不会留下
  半条 processing run。
- Semantic exchange 与全部 candidate 仍原子写入；candidate 唯一键失败时 exchange
  和已插入 candidate 一起回滚。
- ASR hypothesis、alignment token 和 source trace，以及 diarization turn 与 source
  trace，继续在各自单连接事务内写入。
- Identity、Evaluation 和 Action 保留原校验、异常类型、排序与返回的
  `sqlite3.Row` 结构。
- 八个 repository 的直接读写结果与 `Database` 门面结果保持一致。
- schema、migration SQL、CLI、HTTP payload 和 workflow 状态没有改变。

## 3. 自动约束

架构与事务测试确保：

- 已提取的 96 个 `Database` 方法不再包含 `SELECT`、`INSERT`、`UPDATE` 或
  `DELETE`；
- SQLite repository 不得反向导入 `Database` 门面；
- 直接 repository 与兼容门面共用真实 SQLite 测试，不用 mock 替代持久化边界；
- 第二批测试直接覆盖 ASR、Diarization、Identity、Evaluation 和 Action 入口。

当前验证基线：

```text
python -m unittest discover -s tests -v  -> 106 tests passed
ruff check src tests                      -> All checks passed
```

本阶段没有改变 schema，没有加载模型，也没有读取 `data/` 中的真实录音。

## 4. 后续

指南中阶段 2B 建议的八个核心业务 repository 已全部完成。兼容门面仍保留 recording、
source/session ingest、backup 等较低层操作，避免在没有稳定调用边界时机械拆成小类；
后续若这些能力继续增长，再独立提取 `SourceRepository`。

阶段 2C 已完成显式打开/初始化入口，并移除 `Database.__init__` 中隐式建目录和
migration I/O。下一步进入阶段 3，拆分质量工作流。
