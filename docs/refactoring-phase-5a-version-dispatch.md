# 阶段 5A：Semantic / Diarization 稳定入口与版本分发

> 完成日期：2026-08-30
> 范围：建立当前生产入口、共享 semantic 输入/证据接缝和显式版本 dispatcher；保留全部旧 service 导入兼容，不在本批移动历史实现体。

## 1. 问题

阶段 5 开始前，生产调用方直接导入 `semantic_v2e02.py`、
`quality_diarization.py`、`quality_diarization_v2d1.py` 等具体版本文件。当前
semantic V2-E.0.2 还直接从 V2-E.0 和 V2-E.0.1 主流程取得 token 输入和对话分组。
这会让“当前版本”、共享能力与历史兼容入口混在同一依赖方向中，也没有一个可以拒绝
未知 `pipeline_version` 的统一 dispatcher。

## 2. 稳定入口

```text
application/
  semantic/
    contracts.py
    versions.py
    pipeline.py
    overview.py
    review.py
    inputs.py
    evidence.py
  diarization/
    contracts.py
    versions.py
    pipeline.py
    timeline.py
    speech_recall.py
    identity_truth.py
    identity_audit.py
    identity_candidates.py
```

semantic 当前默认版本只有 `v2-e.0.2`；dispatcher 仍可显式选择
`v2-e.0` 和 `v2-e.0.1`。diarization 的基础默认入口为 `v2-d`，D.1 speech
recall、D.2 identity contamination audit 和 D.3 identity candidate mining 按职责
提供稳定模块，同时保留版本选择能力。

CLI、七段式质量 workflow 与 Web application use case 已改为依赖这些稳定入口。
原 `services/semantic_v2e*.py`、`services/quality_diarization*.py` 和
`services/speaker_timeline.py` 导入路径继续可用，既有测试与外部调用不需要立即迁移。

## 3. 公共能力方向

`application.semantic.inputs` 现在拥有：

- recording/session 目标与 V2-C/V2-D run 解析；
- committed token、不可变 source ref 和 speaker attribution 的读取。

`application.semantic.evidence` 现在拥有不绑定具体 semantic 主流程的对话证据种子：

- token gap 分组；
- utterance、ASR disagreement、uncertainty 与 review clip；
- source ref 去重和信息字符判断。

V2-E.0.2 直接依赖这两个公共模块，不再导入
`services.semantic_v2e0` 或 `services.semantic_v2e01`。V2-E.0.1 的 fixture
对比确认新证据模块输出保持一致。

## 4. 版本约束

- semantic 默认：`v2-e.0.2`；
- diarization 默认：`v2-d`；
- 历史版本只能通过明确值选择；
- 未知版本抛出包含支持列表的 `ValueError`，不回退到当前或旧版；
- stable overview 根据最新完成 run 的 `pipeline_version` 选择历史 presenter；
- 当前 V2-E.0.2 overview 遇到旧 evidence format 时明确要求走 dispatcher。

## 5. 验证

- 新增 `tests/test_semantic_dispatch.py`：默认/历史/未知版本、stable pipeline、
  最新版依赖方向和 V2-E.0.1 证据 fixture 一致性；
- 新增 `tests/test_diarization_dispatch.py`：D/D.1/D.2/D.3 显式映射、默认版本和
  未知版本失败；
- 原 semantic、diarization、workflow、CLI/Web 兼容测试继续通过；
- Python 全量 131 个测试通过；
- `python -m ruff check src tests` 与 `git diff --check` 通过。

## 6. 后续结果

上述阶段 5B 工作已完成：历史 semantic 实现、diarization 职责实现和旧 service
兼容层均已归位，历史 fixture 与架构约束通过。详见
[阶段 5B 实现归位记录](refactoring-phase-5b-legacy-and-diarization.md)。
