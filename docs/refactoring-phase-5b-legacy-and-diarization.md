# 阶段 5B：历史 Semantic 与 Diarization 实现归位

> 完成日期：2026-08-30  
> 范围：在阶段 5A 稳定入口和版本 dispatcher 之下迁移实现体；保留旧 service 导入路径、协议、持久化版本和历史 run 行为。

## 1. Semantic 版本归位

Semantic 实现现在按“当前实现 / 冻结历史实现 / 共享能力”组织：

```text
application/semantic/
  current.py
  contracts.py
  evidence.py
  inputs.py
  overview.py
  pipeline.py
  review.py
  versions.py
  legacy/
    v2e0.py
    v2e01.py
```

- V2-E.0 与 V2-E.0.1 的冻结实现迁入 `legacy/`；
- V2-E.0.2 迁入 `current.py`，不再通过旧版 service 主流程取得公共能力；
- dispatcher 直接选择 application 实现；
- `services/semantic_v2e0.py`、`semantic_v2e01.py`、`semantic_v2e02.py` 只保留显式兼容导出。

旧测试仍通过原 service 路径导入公开对象，因此兼容层不是未覆盖的临时别名。测试同时固定兼容导出与新实现为同一对象，并继续运行历史 overview、manifest、review 和导出 fixture。

## 2. Diarization 按职责归位

Diarization 实现不再按 service 文件版本号堆叠，而是位于：

```text
application/diarization/
  base.py
  timeline.py
  recall_pipeline.py
  recall_review.py
  manual_identity.py
  truth.py
  identity_audit.py
  identity_candidates.py
  pipeline.py
  versions.py
```

职责分别覆盖基础说话人 run/token 归属、时间轴展示、D.1 speech recall 与审核、人工身份区间、review truth、D.2 contamination audit 和 D.3 candidate mining。application 模块之间只按职责依赖，不再反向依赖对应的版本化 service 文件。

原 `quality_diarization*.py`、`speaker_timeline.py` 和 `manual_identity.py` 继续存在，但只显式重导出公开 API。CLI、质量 workflow 和 Web use case 已在 5A 使用稳定 application 入口，5B 没有改变 URL、命令参数或持久化格式。

## 3. 约束与回归保护

新增架构断言固定以下规则：

- 旧 service 导入路径指向迁移后的同一实现对象；
- semantic 当前实现不导入 V2-E.0/V2-E.0.1 service；
- diarization 职责模块不导入被替代的版本化 service；
- 默认版本和未知版本失败语义继续由 5A dispatcher 测试覆盖；
- 使用模块级输出目录的测试改为 patch 真正拥有该状态的 application 模块，避免误测兼容 façade。

## 4. 验证

- `.venv\Scripts\python.exe -m unittest discover -s tests -t .`：134 个测试通过；
- `python -m ruff check src tests`：通过；
- `git diff --check`：通过；
- application/CLI/Web/workflow 中没有残留对本批版本化 service 的生产导入。

本阶段没有读取真实录音、运行 ASR/说话人模型、改写数据库或访问网络。

## 5. 阶段结论

阶段 5A 建立稳定入口、共享能力和显式版本选择，阶段 5B 完成历史实现归档、diarization 职责下沉及兼容层收缩。阶段 5 验收条件已全部满足，下一步进入阶段 6：拆分 CLI 与运行时装配。
