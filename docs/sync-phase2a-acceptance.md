# 手机—电脑同步第二阶段 A（电脑端）

后续本轮收尾与重新执行证据见 [sync-phase2a-closeout.md](sync-phase2a-closeout.md)。下文保留上一轮历史结果。

2026-09-26。**部分完成 / 未整体验收通过**；本轮源码、测试、脱敏验收材料正常提交并推送 `origin/master`，完整最终 SHA、GitHub 链接和远端比较结果见交付回复。手机仍有 3 项 lint，真机多场景性能对照和部分故障矩阵未执行，不以宿主通过代替。

## 基线、变更与边界

电脑起始 `895c294f32480b7b34817e9dbab86b1a86ddf443`，手机起始 `763f95a01bac7b0ff5e8ba5bc45bd0b8340e8a99`，均为已交付第一阶段 `master`。开始工作区干净，fetch 后无更新；无适用 AGENTS.md。旧文档已经把“尚未提交”纠正为当时撰写记录的历史时点。本文所在提交即电脑侧本轮测试/审查树；未强推、回退或覆盖真实数据。

电脑修改：

- `src/allday_asr/v3/interfaces/transfer/http_handler.py::_handle`：成功请求 start/end 改 DEBUG；失败/未完成响应 WARNING，仍含 request_id、client_id、安全路由、方法、状态和耗时，500 安全异常栈保留。
- `tests/test_transfer_tls_admission.py::test_successful_polling_is_quiet_at_info`：12 次真实 HTTPS/socket 请求（dispatch 返回体受控）断言没有成功 INFO 或 WARNING；原慢握手、上限/释放、shutdown、500 脱敏断言保留。
- `tests/test_sync_phase2a_recovery.py`：真实 core/SQLite 应用合成标注后丢弃首次响应，用同一 operation_id 重发，检查回执相同、utterance revision 和 annotation_facts 数量不增加、存储的原操作一致。不是只检查返回码。
- `tools/sync_phase2a_device_receiver.py`：独立 loopback:19100 的正式 TLS Receiver、DeviceGateway、真实 trust/auth、review、V3 ingest、UploadStore；合成数据全部位于新的忽略 outputs 子目录，仅延迟正式 append_chunk，记录合成 operation_id/时间/字节/完成状态。拒绝复用已有输出目录，避免旧完成记录伪造新成功。

隔离服务从手机独立 HUKS key 的公共部分本地引导测试设备凭据；不覆盖 Passkey 注册仪式。网络业务仍执行生产 challenge/signature 和信任校验，不关闭 TLS、不会复用 challenge。测试 CA/私钥、真实或合成数据库、音频、HAP、原始报告均不提交。

手机原链路 `synchronize → 整批 audioBackup → metadata sync` 改为 ViewModel 所有的两通道协调器：轻量单消费者、合并触发、每轮 4 批继续运行；备份每次一个会话，Wi-Fi 门禁。保存仅等本地事务，回执/投影/游标短事务原子应用；缺资源/前置操作只挡相关分支；相同受信配置共享连接准备，各 HTTP 独立资源/签名。暂停持久化，前台/网络事件不能绕过；当前轻量批次/当前备份会话收尾后停止。静默同步仅在前台派发，错误分通道持续显示。详细源码映射、生命周期和取消边界见手机 `doc/SYNC_PHASE2A_ACCEPTANCE.md`。

本轮真机还定位并修复两项正确性问题：空增量页清理已应用 outbox 后，UI 漏掉 pending 数变化；多分片上传混用显式偏移和描述符游标，导致重读并触发真实服务器 SHA-256 422。手机现在每片都按已确认 offset 读取；`check-upload-offset.cjs` 在第一阶段基线上哈希断言失败（exit 1），修复后首次/断点多分片及句柄释放通过（exit 0）。没有绕过服务端校验。

## 实际测试

Windows 10.0.26200；项目 `.venv` Python 3.12.10、pytest 8.4.2、ruff 0.16.5。

```powershell
.venv\Scripts\python.exe -m pytest tests/test_transfer.py tests/test_device_auth.py tests/test_v3_device_sync.py tests/test_v3_device_annotations.py tests/test_v3_device_reviews.py tests/test_review_audio_guards.py tests/test_review_audio_boundaries.py tests/test_phone_voice_review_flow.py tests/test_transfer_tls_admission.py tests/test_sync_phase2a_recovery.py -q --junitxml=outputs/sync-phase2a-pytest-final.xml
.venv\Scripts\python.exe -m ruff check src tests tools/sync_phase1_test_receiver.py tools/sync_phase2a_device_receiver.py
```

结果：**70 passed / 11.18s，exit 0**；ruff **All checks passed，exit 0**。日志/XML 位于上述忽略路径。新去重测试最初直接 import TestCase 导致附带收集 28 个已有用例，已改为模块引用；最终 70 数量不包含这类重复。第一次 69 项是尚未加入去重用例的中途结果。

手机最终工程/真机汇总：宿主 **142/142**（entry67、phone75），exit0/25.468s；13 个业务 Node 检查各 exit0，覆盖原事务/审核/连接、协调器、依赖、重开恢复和上传偏移。正式 `product=phone clean assembleApp` **exit0/12.551s**，HAP 无测试 rawfile，已覆盖安装恢复正常入口。原有两个隔离 UI 用例重跑 **2/2、exit0**，报告 `tests/ui/reports/2026-09-26-15-04-32/summary_report.html`。真实 Code Linter **exit1**，剩余 HomePage:14、MorePage:6、PeoplePage:19 三项 avoid-overusing-custom-component-check；同环境恢复两个 UI 修改文件到基线，其余三文件未改，测得 7→3，未屏蔽规则。

被测树边界：成功生产真机包含 offset 和空页清理修复；随后手机补两行“已延后的变更不能被下一批定向刷新遗漏”的保护，并通过实际交错业务回归、最终142宿主、lint及正式构建，未再次整轮真机。电脑70项业务测试之后仅修改独立 receiver 工具的输出目录保护，最终 ruff 重跑通过。

手机环境：Studio Node24.14.1、SDK API26/platform26.0.0/26.0.0.32；SQLite 宿主 Node24.19.0；DevEco Testing Python3.12.10、Hypium/xDevice6.1.0.210、hdc3.2.0c；真实 PLR-AL00/HarmonyOS7.0.0.109 SP6。原样例本轮 1/1，报告手机 `tests/ui/reports/2026-09-26-13-58-55/summary_report.html`。

## A—H 判定

|项|结果范围|
|---|---|
|A 备份中轻量优先|生产真机 1/1 通过，取得真实 applied ID、native/界面待同步归零、上传仍未结束，随后哈希和 manifest 完成。使用正式生产备份/认证/协议，清单装配为合成注入|
|B 依赖隔离|宿主业务集成通过，缺资源分支保留、就绪分支先行、依赖满足/批上限后续跑；非全部真机人物组合|
|C 离线恢复|3 条本地提交、真实 SQLite 重开、正式协调器自动收敛及 ID 保持通过；原生离线保存/重启另列；真实接收端离线→恢复的完整原生自动收敛尚未执行|
|D 合并/生命周期|100 触发/单消费者、继续触发不丢、退避、暂停持久化顺序、监听注销/旧回调、非 Wi-Fi 门禁通过；全部原生重配对交错未测|
|E 丢回执/错误|真实 core 去重通过；手机受控传输验证 400/401/403/500、TLS/身份和仅只读 status 超时重发现；原生静默丢包注入未测；审核决定不自动重发|
|F 失败隔离|受控双通道回归通过，备份错误不抹掉轻量成功；原生故障全过程未测|
|G 交互性能|切页、保存、两秒本地播放、控件内滚动有真实上传进度证据；不等于帧率/无卡顿。基线/改后少量/大量历史/多会话各20样本、P50/P95/max、帧数据均未测|
|H 工程与正确性|70 pytest、手机142宿主、业务回归、独立 native schema10→11；原审核完整播放/证据/未知结果保护保留。lint3项未过，正式恢复详见下节|

真机中间失败保留，未抹除：网络信息权限缺失、合成路径不符合 manifest 规则、已标注页面需进入修改模式、UI 空页清理遗漏、长 WAV 测试参数误用、Hypium 小写方向、Windows 证据文件替换瞬间共享冲突，以及实际多分片 SHA 错误。恢复按真实原因修复或调整测试装配，不降低业务断言。

## 脱敏证据、复现和恢复

最终生产真机报告：手机 `tests/ui/reports/2026-09-26-15-01-47/summary_report.html`，**1/1，exit 0**。新合成操作 `01M3E8AMNZ6SM4E81NRHQKNZ00`，applied 时间 `1790406120.5993392`（Unix 秒），回执时已上传 `589824` 字节且未完成；开始观察 `262144` 字节，完成切页/保存/播放/3 次控件内滚动后 `2621440` 字节且未完成；最终 `8388652` 字节，录音 SHA-256 与 manifest completed 均通过。该次从前次正确上传的 262144 字节继续，保留生产断点语义；过滤旧回执，只接受本次新 ID。

驱动保存开始/观察已保存/交互结束分别 `1790406119.9420917`、`1790406120.2587852`、`1790406144.411466`。这是一次驱动观察，不能代替业务提交计时或性能对照。完整原始证据在手机忽略的 `outputs/sync-phase2a-production-evidence.json`，含之前失败尝试的合成记录；本文只摘录最终成功操作。

恢复后数据保护：本轮 before/after 三目录 **1892 文件、3614605522 字节，逐文件 SHA-256 差异 0，exit0**。手机 `outputs/sync-phase2a-data-integrity.json` 记录摘要；私有导出留在忽略目录。测试 RDB/cache/rawfile、测试 HUKS alias 和两个 receiver/测试 rport 已清理；正常入口与正式 HAP 已恢复，不启动真实业务同步。没有卸载、清真实库或删除用户录音。

最小复现：参见手机 `tools/quality/sync-phase2a-device/README.md` 和 `build-sync-device.ps1`。先只读备份真实业务目录，构建临时入口，导出独立 HUKS 公钥；本仓库运行：

```powershell
.venv\Scripts\python.exe tools/sync_phase2a_device_receiver.py --public-key '<手机仓库>\outputs\sync-phase2a-private\public.txt' --output outputs/sync-phase2a-device-new-run
```

手机将 loopback:19100 rport 到服务，临时 rawfile 携带独立公共 CA/配置，运行原 DevEco Testing runner `run_phase2a.py`。完成只清理明确命名的测试库/cache/alias/原始生成 rawfile，恢复正式入口、clean 正式 phone Debug 并覆盖安装，三目录 before/after SHA-256 比较后只撤销两条测试 rport，停止自己创建的 receiver。禁止卸载、清真实库、断 HDC 网络、删除真实录音或替换配对。

## 交付

提交前两端 `git diff --check` 和暂存内容复核；明确提交源码、测试、本文及手机验收文档，排除 outputs、数据库、音频、密钥、HAP 和 tests/ui/reports。正常 `git push origin master` 后比较每端 `git rev-parse HEAD` 与 `git ls-remote origin refs/heads/master`，读取远端树核对本轮文件；最终回复提供完整 SHA、GitHub commit 链接和实际状态。未完成项按上表保留，不能宣布整阶段通过。
