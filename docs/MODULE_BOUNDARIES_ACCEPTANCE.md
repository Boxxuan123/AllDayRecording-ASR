# 模块边界、版本与有限验收（2026-10-09）

保持现有单进程、共享 SQLite、部署及仓库；没有模块 SemVer。未训练模型、改阈值/两窗口、人工真值、生产画像或冻结评估集。原工作区未提交整改保留并合并验证；只有现用数据库兼容所必需的既有迁移30与校验器随本轮提交，其他整改不混入。

## 现状与复用

Phone 与实际手表 entry@watch 同属 AllDayRecording，common HAR 随 AppScope 绑定发布，版本 1.0.2 / 1000002 / build 3（原1.0.1）。PC 前端随 Python 包共用 0.6.1（原0.6.0）；npm 0.0.0 为私有脚手架字段，不承担发布版本。
协议保持 Watch 1、PC transfer 2、V3 contract 3.8.0 / projection 5；契约 core 27 不等于运行库迁移号。现用库迁移30，Phone投影库19。只读核对证实现用库已到30，因此复用原工作区既有迁移30与迁移器并发校验（SQL/校验和不变），作为本轮发布必要依赖；没有另造迁移。生产库未写入。
复用 digest/manifest/可靠接收幂等、备份准入、持久 job/stage/attempt、输入revision、CAS、proposal/operation、Daily队列和有限SDK子进程。自动处理没有增加整场人工确认；Calendar Kit闭环未改造。

## 六类职责与状态归属

| 职责 | 调用入口、写入责任与状态 |
|---|---|
| 录音与原始资料 | Watch音频生命周期、PhoneRecordingService、ingest/catalog/evidence：原音、manifest、副本、录音生命周期 |
| 同步传输 | WearEngineTransport、PhoneV3UseCases.synchronize、DeviceGateway、UploadStore：可靠接收、cursor/receipt，不承担AI成败 |
| 转写与统一时间线 | AdmissionService、DurableProcessingService、native adapter：处理条件、job/stage/attempt、artifact、utterance revision |
| 人物识别与审核 | PeopleIdentityMixin.analyze、infer_product_self、既有审核命令：识别权限、画像学习权限、人工事实分别表达；画像归people仓储 |
| 事件与任务 | SemanticEventExtractionService、knowledge/reminder命令、CalendarReminderScheduler：proposal、event operation/revision、任务确认和Calendar投影 |
| 总结与人物记忆 | DailyInsightService、recompute_summary、PersonMemoryService：事件快照、总结修订、人物记忆证据，不写原音或同步状态 |

页面沿用应用命令；没有新增万能总服务或全面搬迁目录。

## 版本、诊断和追溯

Hvigor自动产生忽略的GeneratedBuildIdentity.ets，Vite在清理输出前固定来源身份并产生build-info.json，setuptools build_py在wheel注入_build_info.json：发布版本、完整Git hash、UTC构建时间、dirty。源码不写自身提交hash；最终提交后再构建，确认包内hash匹配且dirty=false。
源码运行诚实标记source-checkout、built_at=null、实际dirty；compose时固定身份并记后台日志。旧包无信息unknown。
查看：PC设置→版本与诊断→复制，GET /api/v3/diagnostics（桌面认证），CLI allday-asr status；Phone更多→版本/诊断；Watch开发者页→版本/复制。
他端build来自实际Wear Engine消息或已认证PC status。Phone有能力时用既有设备签名上报；body限8192字节，失败不撤销同步。未报告unknown；120秒过期offline_or_stale；不拿本端版本填他端。发布版本不参与协议兼容性拦截。

接收回调排队失败只返回automation.queue_failed，保持ingested/already_ingested。stage_results沿用持久workflow状态并记输入revision/当前证据digest：恢复跳过成功阶段；真实证据变化才失效。禁学会话仍推断、不写画像；正常学习和known-person rematch保留实际匹配快照。

来源接口：GET /api/v3/provenance/{generation|artifact|speaker-run|identity-match}/{id}。
复用现有JSON字段和内容寻址库，记录run/generation/stage/attempt ID、输入ID/revision/full snapshot+sha、实际模型、精确请求/指令/schema的receipt路径+sha、构建、完整规则/配置、画像快照、输出结构版本。模型调用构建在immutable request receipt中，发表构建与缓存调用构建分别可查。旧行无来源为historical_unknown，不回填。人工编辑保留严格命令结构，不伪装AI结果。

局部重算命令（目标需替换，示例日期仅说明语法）：

    allday-asr recompute run --state-dir <V3_STATE> --execution-id sample-events-1 --stage events --target <SESSION_ID> --timeout-seconds 180
    allday-asr recompute run --state-dir <V3_STATE> --execution-id sample-summary-1 --stage summary --target 2026-09-01 --timeout-seconds 180
    allday-asr recompute status --state-dir <V3_STATE> --execution-id sample-events-1
    allday-asr recompute cancel --state-dir <V3_STATE> --execution-id sample-events-1

最多8个独立目标，默认总期限180秒/最大900，每项持久最多2次尝试；同ID同范围，结束重放不重做。失败/取消/超时有结束状态。事件只读已有转写，更新自动生成记录时保留身份/CAS，不覆盖人工确认；总结只读已有Daily事件/任务，跳过事件分析；不重传或ASR。
Git/发布身份不参与语义缓存失效。Daily自动入口跳过仅生产配置修订而源摘要未变的历史结果/失败任务；inventory仍可诊断旧规则，显式重算可用新规则；真实证据修订维持既有自动规则。

## 有限验收清单、命令与结果

| 检查 | 命令/方式 | 期限与结果 |
|---|---|---|
| 同步/恢复/幂等、禁学、局部重算、来源/旧行、迁移和模块边界 | python tools/verify_module_boundaries.py --output outputs/module-acceptance-schema30-final | pytest180s/ruff60s；247条通过（含真实HTTP签名诊断上报与29→30副本读取）；修改Python Ruff通过 |
| 最新未提交实现合并兼容 | 临时integration副本定向测试 | 单批120–180s；131条来源/任务、77条升级/队列通过；首次其余工程定向回归257条通过 |
| 人物补充 | v34/self_identity/module_boundaries固定样本 | 49条通过 |
| 前端 | npm test；npm run build | 每条120s；测试、类型检查、Vite通过 |
| 包装身份 | python setup.py bdist_wheel --dist-dir outputs/delivery | 120s；本机依赖，不访问模型 |
| 设备端host | check-runtime-diagnostics/calendar-reminders/sync-transport/phase2-original-sync | 每条30–45s；4组通过（临时SQLite/原生替身） |
| 手机/真实Watch | Hvigor product=phone/default assembleApp --no-daemon | 每个240s；编译、打包、签名通过 |
| 实机可用性 | hdc list targets | 10s；一次[Empty]，未反复等待 |

Linter不宣称全绿：8处await-thenable报告，原提交已有相同语句，SDK标为disabled；本轮无新增。构建保留既有SDK弃用/设备API警告；新增剪贴板调用另有SDK异常检查警告，调用方处理Promise拒绝。
未验证：实机安装、Wear Engine实际链路、系统剪贴板UI；HDC无设备。没有调用真实生产AI或全量历史回放。临时迁移副本旧数据可读；未迁移生产库。
最终提交、远端一致性和提交后包内核对见忽略的outputs/delivery/acceptance.json及交付回复。原工作区未被改写；回写合并与更新原master被自动审批拒绝，等待批准。原提交的工作树HEAD仍落后，交付工作树HEAD已与远端一致。
