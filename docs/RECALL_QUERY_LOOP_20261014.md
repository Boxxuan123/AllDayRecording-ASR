# AllDay 自然语言历史回忆闭环验收（2026-10-14）

本轮从最新远端开始：ASR `9ac56f49a66fc1e4982c786ccd222f0ecea993a0`；Phone `251f3b6b0000ca10d4fdba2a4c704dee11fab1c3`。原工作区保留未提交修改，所有修复在 `.task-worktrees/recall-query-loop` 完成。没有读取生产聊天、运行历史重算或安装设备包。

## 原因与真实代码链路

六项审计问题在上述基线均仍存在：`recall_language.plan()` 生成中文二字片段；`intent()` 把“聊了什么”当 quote；`HistoryRecall.query()` 只接收显式筛选、未理解自然语言人物/日期；SQLite 以 OR 子串检索；`SourceIdentities.people()` 未读人物已有别名；`_summarize()` 未把有效 claims 更新到 answer。

手机新版还默认 `summarize=false`，`productRecallAnswer()` 主要显示检索状态。生产代码链路已静态核对：`PhoneV3RecallPage → PhoneRecallSearch → PhoneV3ViewModel.historyRecall → PhoneV3UseCases.historyRecall → ComputerTransferHttpClient.recall → 已配对设备 POST /device/v3/recall → dispatch_review_post → DeviceGateway.recall → HistoryRecall.execute/query`。手机沿原认证和查询取消/分页/引用路径；本轮没有在真实手机抓取请求或安装运行修复包。

## 查询理解前后

| 提问 | 修复前 | 修复后 |
|---|---|---|
| 昨天我跟张sir聊了什么 | quote；人物为空；默认最近30天；含“天我”“我跟”等 n-gram | conversation；仅精确匹配已确认姓名/最新 profile aliases；手机 IANA 时区的昨天完整自然日；本人＋目标人物；主题词为空 |
| 同一句中的张sir没有可信别名 | 可能跨人物搜索并返回无关原话 | needs_clarification；明确缺少可信映射；证据/模型/远端调用均为零 |
| 同名或同别名多人 | 无消歧 | 返回候选，手机选择 person_id 后重试；选择必须属于该提及的可信候选 |
| 为什么不选软件工程 / 事情经过 / 后来如何 / 最终决定 | reason / timeline / latest 原路径 | 保留查询类型，去掉语法词；无任意中文 n-gram，真实主题短语使用 AND 约束 |

“张sir”仅用于说明原问题，生产规则中没有张sir、杭电或任何测试人物/主题词表。新增理解层是确定性语法解析，不调用模型、不创建身份库。人物仍来自 `persons.user_confirmed=1`、最新 `person_profile_revisions.aliases_json`、已有人工来源账号关联和来源本人方向。

时间支持昨天/前天/今天、上周（周一至周日）、本周、完整年月日、当前年“月日”、日期范围、单日 `09:00到10:00` 和明确时段。自然日以当地下一日零点为右边界，转换成 Unix 秒闭区间。跨月/跨年和 DST 的 23/25 小时自然日通过测试。输入时刻处于 DST 歧义/不存在的边界、多个时间描述或不支持的“去年/上个月”等表达时明确拒绝；不忽略时间。显式日期/平台筛选与提问取交集，冲突拒绝，人物冲突要求选择。

## 检索与回答约束

- 无主题时按人物、发生时间、来源账号和会话身份检索；有主题时所有真实主题短语必须匹配。消息使用 `sent_at`，录音使用 utterance `start_at`，不使用导入/捕获时间。
- 私聊由目标人物已确认的来源账号锚定 `(platform, source_account_id, conversation_id)`，取回本人和对方；至少观察到双方才作为交流证据。不同平台、账号命名空间、会话保持独立。
- “我与某人交流”的群聊仅取双方同群的明确 `reply.record_id` 对；其他人、未知账号、引用中的作者、无回复关系的旁支消息不会成为这段交流的证据。引用打开后仍保持相同时间和参与约束。
- 人物发言查询的录音身份沿原人工/human_cluster 确认。仅说话人确认不能证明本人参与交流，因此对话回顾不纳入这种录音，并明示关系证据不足。原有不指定人物的纯原文检索仍能展示“未知说话人”，不把其归为具体人物。
- 检索无结果或来源离线不扩大人物/时间/权限。缓存最多扫描500条选定会话记录、返回100条聊天匹配；总体上限300条、远端6请求/100记录，本轮对话远端最多核对前两个已确认会话，并明确覆盖缺口。
- 归纳复用原 ModelExecutionRunner：每个问题最多1次、无重试、最多20条选定证据、最多20秒，查询总预算30秒。模型只接收已选证据及约束；无查询理解模型，没有模型生成 person_id 的步骤。
- 引用 ID 必须属于实际模型输入；quote 必须为一条原文的真实子串。校验后 claims 组成主 answer；手机以 available claims 的内容生成主回答，同时显示 unknown、来源 coverage 与每项引用按钮。空 claims 为 insufficient；超时/不可用/无效引用为 unavailable，清空结论，保留受约束原文。

虚构闭环成功时，手机主回答是：**“你们讨论了样品检查，约定周五十点检查，由本人准备清单。”** 绑定对方 `chat:rec:fixture-1001` 和本人 `chat:rec:fixture-1002`，并显示其他会话/实际执行状态尚未确认。

推测类 claims 在主回答中保留“推测：”标记；显式 literal 查询保留调用者的原话、人物和日期筛选，原文内的引用作者/相对时间不能改写查找约束。

模型失败时主回答是：**“无法生成归纳（模型不可用、超时或证据校验失败）；以下仅为符合本次约束的原文。”** 证据不足时明确“无法生成可靠归纳”；人物无可信别名时明确要求确认人物；指定范围无记录时明示不能据此判断没有发生。来源离线与来源校验/取回失败使用不同覆盖文案。

## 文件清单

ASR：

- `domain/recall_query.py`：人物、时区日期、会话关系、主题与澄清结果。
- `domain/recall_language.py`：conversation 意图；删除 n-gram。
- `domain/recall_conversation.py`：双方参与与群聊回复关系的纯验证。
- `adapters/sqlite/recall_conversations.py`：发生时间内的可靠会话选择及有界读取。
- `adapters/sqlite/recall_sources.py`：AND 主题、会话检索、录音关系边界、回复来源字段。
- `adapters/sqlite/recall_state.py`：远端授权会话与引用上下文过滤。
- `adapters/sqlite/source_identity_repository.py`：复用已有 aliases_json。
- `application/history_recall.py`：理解层接入、有效 claims 主 answer、失败/不足状态。
- `application/recall_retrieval.py`：远端双方消息与人物/日期/会话验证。
- `adapters/codex/recall_model.py`、`ports/history_recall.py`：原执行器提示与边界接口。
- `contracts/v3/schemas/history-recall.schema.json`、`manifest.json`：可选 timezone、conversation 和候选响应；未改数据库/投影版本。
- `tests/test_recall_query_loop.py`、`tools/recall_query_phone_fixture.py`：隔离验收和 JSON-lines 手机桥接。

Phone：`domain/PhoneHistoryRecall.ets`（类型、当前当地自然日、回复来源复制）；`presentation/PhoneProductRecall.ets`（主回答与状态）；`presentation/PhoneRecallSearch.ets`（系统时区、默认归纳、已有结果页中的同名选择）；`contracts/v3/source.json`（规范指纹）；`tools/quality/check-recall-query-loop.cjs`（真实 UseCases 与最终文案测试）；两仓验收文档。

维护复审：新增隔离测试模块561行，集中使用同一虚构环境检查整条闭环；HistoryRecall 当前518行，超过 Phone README 的500行复审信号、低于 ASR 强制600行门禁；新增理解及会话验证独立为纯 domain 模块，应用层不导入 SQLite/模型/UI。Phone 未新增状态字段，原搜索组件已有较多状态，本轮仅改请求和回答闭环，不重构页面。

## 隔离验收

后端相关验收 **114 passed**：新自然问题矩阵＋原历史回忆＋冻结原因/经过/最终状态用例＋架构＋双仓契约，零 skipped。另有聊天查询、跨天人物记忆、设备认证及传输 **102 passed**。合计216项；只使用 tmp 虚构 SQLite 与 fake 来源/模型。

正反例检查最终答案和真实模型输入，不仅是数量：三种已确认别名、QQ/微信、不同日期/当地时段、多会话、同名候选、未知别名、无效 person_id、正确人物错误日期、正确日期错误人物、正确人物日期但无交流、双方私聊、群聊他人/引用归属、普通片段不命中、模型成功/超时/无效引用/空 claims、来源离线、伪造远端日期、范围冲突、打开引用不越界。查询前后 Core/Chat dump 和 Phone outbox 不变。

Phone 跨仓虚构 `check-recall-query-loop.cjs`、原 `check-history-recall.cjs`、`check-phone-product-v2.cjs` 均 PASS；真实 UseCases → Python 服务 → 生产主回答函数，含成功、失败、空结果、离线、日期 DST 边界。Code Linter 为0 defects；严格 production Phone Debug/Release 各自 clean 的 ArkTS 编译及未签名 HAP 打包通过。官方 Hypium 新报告 **170/170 PASS**（Watch68、Phone102）；隔离依赖外部联接曾导致 Hypium 未打入ABC，改为本地复制；Phone product 产物先核对哈希，再按现有说明复制到 SDK 固定 `.test/default` 启动路径。生产严格配置保留，SDK/门禁源码未修改，初次超时日志保留。

未验证：真实手机安装/截图/请求与生产 QQ/微信来源；真实模型提供商的语义质量和超时；完整历史的捕获覆盖；设备跨时区实机行为。fake 模型只证明选证、引用验证和答案展示协议，不能证明真实模型已正确概括。真实生产链路本轮 **NOT_RUN**。不重做UI、不改提醒/日历/声纹/人物记忆架构，不新增向量库或GraphRAG。

## 供 ChatGPT 独立审核

审核包应包含本文、两仓相对基线的完整 patch、虚构 success/timeout/invalid/insufficient/unknown/not_found/offline JSON、复现命令和最终完整 commit SHA。请独立检查：身份未解析是否零检索；相同人物/错误日期与正确日期/错误人物是否排除；c2c 是否包含双方且保留来源命名空间；群聊回复及引用身份是否严格；context/远端扩展是否保持硬约束；available claims 是否进入主回答；失败是否清空结论并保持准确 coverage；是否存在测试样例专用词、查询副作用或对生产/原工作区的修改。资料已准备不等于独立审核已经通过。

复现：后端使用 Python3.12，`PYTHONPATH=src;.`、`ALLDAY_HARMONY_REPO=<Phone隔离目录>`，运行上述11个测试模块和 Ruff。手机使用带 `node:sqlite` 的 Node24、`PHONE_TEST_TYPESCRIPT=<已有typescript模块>`、`CHAT_PHONE_PYTHON=<ASR Python>`、`CHAT_PHONE_ASR_ROOT=<ASR隔离目录>`、`TZ=Asia/Singapore`，依次运行三个回忆/产品脚本、Code Linter 和项目官方 hostTest 门禁。Phone build 使用仓库 build-profile.example 的 phone product；构建配置/依赖/日志/HAP 不提交。
