# 离线复现

只使用本仓库虚构 fixtures 和新临时目录。禁止调用 evaluate_isolated_agentic.py 的真实模型 run 子命令；这里仅复用其 seed 与 IsolatedSource。export 工具及测试显式注入 fake、禁止模型/规划器，并验证 Core 与缓存查询前后 dump 不变。

环境：Python3.12；现有 requirements/pyproject 中的测试依赖；不改SDK或模型配置。从本次完整提交检出源码，或者在基线3945ef5e546e139ec19b7e0e9ab0dda21062eb1d上应用审核包 change.patch。Phone参考15ecfc60d71ad976ae2ee9b4a7c8d583326b6ede。

```powershell
$env:PYTHONPATH='src;.'
$env:PYTHONIOENCODING='utf-8'
python -m pytest -q tests/test_agentic_review_fixes.py tests/test_agentic_root_scope.py
python -m pytest -q tests/test_agentic_evidence_reliability.py
$taskTests = Get-ChildItem tests -File | Where-Object { $_.Name -match '^test_(agentic_|recall_|history_recall|investigation_capabilities|isolated_agentic|v3_architecture|v3_contract)' } | ForEach-Object { $_.FullName }
python -m pytest -q @taskTests
python tools/export_agentic_reliability.py --output outputs/my-new-offline-receipts
```

完整后端验证所需的本机跨仓环境如下（其他机器调整安装路径）；本轮只运行一次全量，没有启动生产进程：

```powershell
$env:ALLDAY_HARMONY_REPO='C:\Users\32673\DevEcoStudioProjects\AllDayRecording\.task-worktrees\agentic-rag-v1'
$env:PHONE_TEST_TYPESCRIPT='C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\ets\build-tools\ets-loader\node_modules\typescript'
$env:PATH='C:\Users\32673\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin;'+$env:PATH
python -m pytest -q
```

原28项映射见RESULT末尾。失败诊断日志保留，最终结果以targeted-all.log/full-backend.log为准。case-*.json含实际fake模型输入、步骤及来源返回；标准答案只在RESULT供离线审核。real_model_calls始终0；metrics.model_calls是协议fake轮数，不是真实API调用数。

静态检查只对变更Python文件作为本补丁门槛；ruff.log是额外全仓检查，242项既有问题均来自未改文件，lint-baseline.json已核对。没有添加规则忽略。
