# 可维护性重构阶段 3：显式质量工作流阶段

> 完成日期：2026-08-30
> 前置基线：阶段 2C，110 个测试通过
> 交付基线：116 个测试通过
> 范围：拆分 V2 质量工作流的类型、阶段 runner、编排和兼容入口

## 1. 结果

质量工作流现在位于 application 层：

```text
application/workflows/
  quality_models.py   # WorkflowContext、StageResult、ReviewReason
  quality_stages.py   # 具体阶段 runner 与复用匹配
  quality.py          # 持久生命周期、顺序编排与最终汇总
```

原 `services/quality_workflow.py` 从 749 行降到 17 行，只保留
`run_quality_workflow`、`QualityWorkflowSummary` 和新显式类型的兼容导出。CLI 及项目外
既有导入路径无需同时修改。

编排顺序由架构测试固定为：

```text
verify_admission
  → resolve_or_run_asr
  → resolve_or_run_diarization
  → run_optional_speech_recall
  → run_optional_identity_audit
  → inspect_identity_mining_state
  → resolve_or_run_semantic
```

公开入口 `run_quality_workflow` 现在为 81 行，阶段编排函数为 83 行；二者不再内嵌
复用查询、D.1/D.2 阈值计算或 D.3 候选审核细节。

## 2. 显式类型与阶段契约

- `WorkflowContext` 固定 workflow/session/recording、输入指纹和准入模式。
- `StageResult` 显式返回 stage、run ID、status、reused、持久摘要和审核原因。
- `ReviewReason` 固定 code、stage、message 与结构化 details，并提供兼容 JSON 转换。
- `WorkflowStageResults` 集中汇总复用阶段与 D.1/D.2/D.3 审核原因。

各阶段的失败边界保持原语义：

| 阶段 | 复用/执行 | 失败语义 |
| --- | --- | --- |
| Admission/Integrity | 每次重新验证 | 阻断模型阶段，workflow run 标记 failed |
| V2-C ASR | 完成 run 复用；失败/运行中 run 续跑 | 基础阶段失败，workflow failed |
| V2-D Diarization | 配置和上游 ASR 一致时复用 | 基础阶段失败，workflow failed |
| V2-D.1 Speech Recall | 匹配上游与设置时复用 | 转成 `v2d1_failed` 审核原因，不阻断 E |
| V2-D.2 Identity Audit | 有冻结 speaker truth 才运行 | 转成 `v2d2_failed` 审核原因，不阻断 E |
| V2-D.3 Identity Mining | 只检查既有人工目标与候选 | 不自动认人；待审核或 not applicable |
| V2-E Semantic | 有 committed token 时复用或运行 | 无 token 正常跳过；执行失败则 workflow failed |

没有引入通用 DAG、插件框架或自动重试框架；runner 仍是具体、可读的 Python 函数。

## 3. 兼容性与自动约束

以下行为保持不变：

- workflow config、pipeline version、processing run 父子关系和输入指纹；
- `QualityWorkflowSummary` 字段及 `review_reasons` 的字典结构；
- persisted `stages`、`enhancements`、`review` 和 `workflow_state` JSON；
- production/shadow 准入、ASR checkpoint 续跑、D.1/D.2 非阻断增强；
- CLI 输出、Web payload 和云端禁用边界。

新增测试覆盖：

- 七个阶段的静态编排顺序；
- 旧 service 文件不得重新承载业务函数；
- `ReviewReason` 结构化序列化；
- 匹配失败 ASR run 的 resume ID；
- D.2 异常转为审核原因；
- D.1 异常不会阻断已有 committed token 的 semantic 阶段。

当前验证基线：

```text
python -m unittest discover -s tests -v  -> 116 tests passed
ruff check src tests                      -> All checks passed
```

本阶段没有改变 schema、migration SQL、模型参数、阈值、CLI/HTTP 协议或数据边界，
也没有加载真实模型或读取 `data/` 中的真实录音。

## 4. 后续

下一步按指南进入阶段 4：先拆 Web 后端的认证、路由、job 和 presenter，再拆前端 API、
state、audio 与 view；URL、认证方式和 payload 必须继续兼容。
