# 精简独立审核入口

基线 ASR ecd5c9eb2576d141e6f984aa9797d385cf926535；Phone 7032b093bb9150c86111e8cdc63ae47acc71234f。

重点检查：

1. `recall_directory.py` 只派生来源目录；没有人物卡门禁。原生key绑定dataset/platform/account/conversation；完整分页后才自动确认唯一，不完整页跨查询有界续取。
2. `recall_understanding.py` 限定模型候选及原始硬约束，源联系人/统一人物独立。选择请求校验owner和已返回key；不写人工人物关系。过期上下文不授权新范围。
3. `recall_conversation.py`、`recall_state.py`、`recall_conversations.py` 保持发送者、来源、发生时间及会话真实归属，私聊取双方，群聊不归给群名/引用者。
4. `recall_summary.py` 保留逐字引用验证与有效citation集合；单方不足和模型失败不返回成功归纳。
5. Phone `PhoneRecallSearch.ets` 选择后复用原qid；`PhoneProductRecall.ets` 主回答使用claims，普通范围不显示ISO/id/枚举，诊断折叠。

复现（虚构DB、模拟模型）：

```powershell
$env:PYTHONPATH='src'
python -m pytest -q tests/test_recall_contacts.py tests/test_recall_query_loop.py tests/test_recall_night_remote.py tests/test_history_recall.py tests/test_recall_corrections.py tests/test_recall_review_regressions.py tests/test_chat_self_identity.py tests/test_chat_time_regressions.py tests/test_v3_architecture.py tests/test_v3_contracts.py tests/test_v3_contract_generation.py tests/test_v3_device_sync.py
python tools/verify_module_boundaries.py --output outputs/recall-contacts-deploy/gates
python tools/sync_v3_contracts.py --phone <Phone隔离工作区> --check
```

Phone设置现有测试环境变量后运行 `node tools/quality/check-recall-query-loop.cjs`，以及官方 `run-code-linter.mjs` / `run-host-tests.mjs`。核验最终展示的内容、原文、同名选择、模型失败与业务outbox未变化，不只统计命中数。

测试：联系人专题34例；相关组合251 passed/1 skipped（外部可选依赖）；项目既有架构/设备/模型门禁247 passed；Phone桥接通过；Code Linter 0 defects；Hypium170/170。组合有重叠，不相加。

未验证：Mac原数据库备注恢复、完整来源覆盖、真实模型语义归纳、手机实机选择/引用/返回。HDC无设备时不能计为安装或连接成功。此文件仅提供审核入口，不声称已获得ChatGPT独立审核。
