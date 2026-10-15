# RESULT — Agentic RAG 0.8.12 证据检索可靠性补丁收尾

本轮继续原隔离工作树中的未提交补丁，没有重建方案。真实模型调用 **0**、token **0**、真实推理 **NOT_RUN**；没有部署或连接生产 QQ、微信、录音库。A/B/C 是公开答案后的工程回归，不是盲测，也不是模型效果成功率。

## 基线与修改范围

- ASR 基线：`3945ef5e546e139ec19b7e0e9ab0dda21062eb1d`；修复版本 0.8.12。
- Phone：`15ecfc60d71ad976ae2ee9b4a7c8d583326b6ede`，NO CHANGE。
- 原工作区修改保留；仅 `.task-worktrees/agentic-rag-v1` 工作树开发。
- 修改限于调查检索/上下文/有界后续、证据诊断、回复引用字段投影、虚构回归、离线回执工具及版本号。基础回忆接口、模型/推理强度、SDK、提示词、协议校验、硬范围模块不变。
- 网关白名单、SDK执行器、协议、硬范围等8个关键文件与基线无差异，完整 SHA-256 见 `unchanged-boundaries.json`。不修改人物、记忆、事项、提醒、日历或声纹。

## 原始阻塞的处理

1. 23项旧测试：构造函数将可选 `person_mention` 默认成空字符串。没有修改授权、person_id 或硬范围；先单独运行两个旧安全文件，32 passed，原23项都执行到断言。
2. 5项新测试：空搜索计划替换为合法 `find_conversations` 动作，只查通用目录词，不含人物答案、record ID、会话路径。评测入口提前调用现有 `checked` 验证 fake 协议。生产协议未放宽。
3. 合法驱动暴露真实缺陷：账号探索只做一跳。现改为在共享动作预算内继续探索新发现的稳定账号，优先新会话，保留上下文动作；不会新增模型轮数或来源预算。
4. 离线测试原先在目录阶段就停止，尚未进入要测的缓存检索路径。将该测试故障注入消息/上下文读取入口，并另增目录离线提前澄清测试，分别验证两种状态。
5. 4个 Ruff E402：将导入移到常量前，无忽略项或 lint 配置修改。

文末逐项列出原28项失败的修复与结果。没有删除旧测试或放宽原断言。

## P1–P4 工程结果

| 项目 | 结果 | 行为与边界 |
|---|---|---|
| P1 过滤 | PASS | 初始主题搜索仍按关键词；可靠字面事件锚点可召回候选；回复/引用或有界同会话上下文不要求重复初始词。硬账号/平台/会话/日期/修订检查保持。相关性标为未确认，不自动等于事实。 |
| P2 后续 | PASS | 第二轮前沿原文稳定账号及事件锚点继续有界展开；陌生案例完成首群→校验群→新账号私聊→次日解释。另有仅远端存在后续的通过回归。消息发送时间决定读取窗口，未把它冒充事件发生时间。 |
| P3 称呼 | PASS（取证与边界） | 私聊目录种子不再锁死历史身份查询；沿已有30日 identity 窗口取回8101/8102。第三人记录可作为待判原文，但不能自动确认或写入身份；人工选择机制及同名账号隔离不变。自然语言身份推理本轮 NOT_RUN。 |
| P4 证据流 | PASS | source_returned、remote_returned、cache_returned、application_received、candidate_seen、application_accepted、hard/soft_rejected、candidate、model_selected分别计数，并记录安全ID/修订/理由/关系ID、每轮入模ID、最终输入数量和停止原因。无正文或内部思维链。 |

阶段计数按平台、来源账号、record ID、revision去重；`observations`记录重复观察次数。各阶段有交集，不可相加作总数；远端/缓存独立计数的并集才是来源返回总量。来源侧SQL未返回的记录不算应用拒绝，不能通过 `omitted_count=0` 声称来源全部召回。诊断事件最多600条，溢出有 dropped_events。

## 修复前后与实际入模证据

以下ID省略统一前缀 `rec:fixture-`。完整原文、来源查询、输入及拒绝理由见离线回执 `case-A/B/C/NEW.json`。

| 案例 | 0.8.11实际最终输入 | 0.8.12离线最终输入 | 核心恢复 |
|---|---|---|---|
| A | 8103,8104,8105,8107,8109 | 8101,8102,8106,8103,8104,8105,8107,8109 | 历史直接称呼8101与对方回复8102；指定日8103/4/5 |
| B | 8201,8202,8203,8204 | 8201,8202,8203,8204,8209,8205,8206 | 次日私聊8205/6，登记3台不等于交付3台 |
| C | 8301,8302,8308 | 8301,8302,8303,8304,8308,8305 | 不含初始指控词的8303解释、8304核验局限、8305后续未知 |
| 陌生气象案例 | 此前未完成合法协议 | 91001,91002,91003,91004,91005 | 跨两群、两账号、次日私聊；排除91006相似编号、91007同名账号、91008窗口外进展 |

四项评价分开：上述四例的核心来源记录找到、程序保留、最终fake输入三个环节均通过；真实模型推理全部 NOT_RUN。fake第二轮仅返回未知，不伪造事件答案、来源断言或身份结论。

公开案例离线标准答案摘要：A须以8101/8102支持称呼账号，再核对目标日照片授权及灰样安排，不能混入同名账号；B登记3台、实际W11/W12签收、W13未发，不能说三台都到借用人手里；C存在指控、分批记录解释及尚缺银行/收款依据，不能确认挪用或合规。新案例答案是换探头后仍偏高、尚不能交付，等待复核；不能借相似编号或同名账号说已交付。这些答案仅离线审核，不进入 fake 计划或模型提示。

新增正反例还验证：英语图书馆记录的原生 reply/quote 关系可保留无初始关键词解释，错误会话/日期仍拒绝；无关系且时间相隔较远的解释拒绝；中文紧邻 LP-984 可识别而 LP-9840 不混入；远端消息晚采集仍按 sent_at 判断；分页不足、来源读取离线、目录离线、撤回记录的状态分别验证。每次端到端回归前后 Core/缓存业务数据库 dump 不变。

### 仍然存在的限制

- C的8306来源已返回，但仍未进入最终输入：首次目标日上下文因 context_scope_or_date 拒绝；后续缓存上下文因 no_bounded_context_relation 软拒绝。它只是第三人复述指控，没有明确事件字面锚点或原生 reply/quote ID，距锚点超过30分钟。这是程序候选过滤缺口，不是来源未找到，更不是模型推理失败。8301已覆盖原始指控，核心解释与未知依据齐全，但不能声称C的每条转述都被找齐。
- A的8106第三人称呼、B的8209闲聊、C的8308字体信息仍可能进入有界上下文候选；保留了未确认标记，程序不宣称其语义相关。第三人语义判别和最终推理仍待独立真实模型验证。
- 确定性跟踪依赖原文字面事件锚点（引号或编号）和稳定来源账号；无锚点、无桥接、目录/缓存未暴露关系、过多分支或预算用尽时保留缺口，不扩大到所有聊天。
- 后续窗口保持既有两天规则并与用户硬日期相交；显式会话/人物范围不能被程序或模型覆盖。
- 公开B/C当前回执的后续有缓存支持；额外远端专用回归证明陌生案例的后续可从远端取回，不等于证明所有远端关系都可发现。
- 不将测试fake的全未知回答当作效果通过；本轮不据此宣称具备真实数据全面试用条件，先交独立审核。

## 验证及性能

- 旧接口/安全范围：32 passed（3.13秒）。
- 调查、基础回忆、权限、隔离执行、架构、契约：311 passed（86.49秒），包含21项本专项回归。
- 受影响Python文件 Ruff：PASS；原4项E402消除。
- 全仓 Ruff额外检查发现242项既有问题，均位于本轮未修改文件，未扩大范围修复；详见日志。
- 全量后端：1426 passed、1 skipped、27 warnings，273.52秒；仅运行一次全量。。
- Phone源码/契约未变；后端完整测试使用该Phone隔离工作树的契约与TypeScript环境。无安装、构建部署或生产同步。

| 案例 | fake调用 | 调查动作 | 检索/来源请求 | 读取次数 | 查询ms | 含建库导出秒 |
|---|---:|---:|---|---:|---:|---:|
| A | 2 | 4 | 6 / 8 | 14 | 204 | 0.515 |
| B | 2 | 6 | 6 / 8 | 10 | 328 | 0.578 |
| C | 2 | 6 | 6 / 8 | 10 | 281 | 0.531 |
| NEW | 2 | 7 | 6 / 8 | 11 | 203 | 0.422 |

保留2次模型、8动作、6检索、12来源、100远端记录、20条/28KB入模和60秒查询上限。回执中达到来源检索预算会标记 source_request_budget 与 followup_bounded_not_exhaustive；关键证据已经入模也不等于全历史完整。上表是离线工程耗时，不代表真实模型性能。

## 复现与交付

依赖沿用仓库Python环境；设置 `PYTHONPATH=src;.`，运行 `python -m pytest -q tests/test_agentic_evidence_reliability.py`。完整复现环境与命令见审核包 REPRODUCE.md（仓库副本：docs/AGENTIC_RAG_0812_REPRODUCE.md）。`python tools/export_agentic_reliability.py --output <不存在的新目录>`只导出虚构离线回执，不启用SDK模型。审核包仅含差异、必要源码、虚构JSON、测试日志、回执和复现方法；不打包SQLite、真实聊天、录音、密钥或环境配置。

完成后停止，下一步交ChatGPT独立审核；不自动开启真实盲测或修复阶段。

## 原28项失败逐项对照

| 原失败测试 | 修复及验证 | 结果 |
|---|---|---|
| `tests/test_agentic_evidence_reliability.py::test_unfamiliar_identifier_followup_and_nearby_explanation` | 改为合法目录查询 fake 计划，生产协议未放宽。 新发现的接手账号继续有界展开，91004/91005 入模。 | PASS |
| `tests/test_agentic_evidence_reliability.py::test_explicit_dates_block_later_messages` | 改为合法目录查询 fake 计划，生产协议未放宽。 硬日期限制保持，后续窗口受限明确记录。 | PASS |
| `tests/test_agentic_evidence_reliability.py::test_source_offline_preserves_bounded_cache_and_reports_gap` | 改为合法目录查询 fake 计划，生产协议未放宽。 将故障注入检索入口，验证缓存与 SOURCE_OFFLINE；另增目录离线提前澄清测试。 | PASS |
| `tests/test_agentic_evidence_reliability.py::test_late_capture_uses_message_time_and_pagination_is_reported` | 改为合法目录查询 fake 计划，生产协议未放宽。 第二账号补取恢复，按 sent_at 接收迟到消息，分页不完整明确报告。 | PASS |
| `tests/test_agentic_evidence_reliability.py::test_withdrawn_cached_record_cannot_bridge_or_enter_model` | 改为合法目录查询 fake 计划，生产协议未放宽。 补取路径现在实际到达撤回记录，来源完整性校验拒绝它。 | PASS |
| `tests/test_agentic_root_scope.py::test_model_target_cannot_replace_original_contact_even_with_known_reference[False]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_model_target_cannot_replace_original_contact_even_with_known_reference[True]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_root_snapshot_cannot_be_changed_via_result_or_dynamic_scope` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_context_and_direct_evidence_outside_root_are_not_read_or_admitted[cache]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_context_and_direct_evidence_outside_root_are_not_read_or_admitted[mac]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_same_target_and_bilateral_messages_still_work` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_root_scope.py::test_directory_refs_and_memory_do_not_bypass_hard_contact` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[False-grants0]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[False-grants1]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[False-grants2]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[True-grants0]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[True-grants1]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c[True-grants2]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes0]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes1]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes2]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes3]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes4]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes5]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes6]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_remote_ignoring_scope_never_admits_out_of_scope_record[changes7]` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_selected_group_keeps_observed_speakers_without_inventing_roles` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
| `tests/test_agentic_review_fixes.py::test_incompatible_grants_never_fallback_to_account_wide_query` | 可选 person_mention 使用空字符串默认值；原安全断言实际执行并通过。 | PASS |
