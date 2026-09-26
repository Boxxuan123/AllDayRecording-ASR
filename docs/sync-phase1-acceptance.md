# 手机—电脑同步第一阶段验收（电脑端）

2026-09-26；**部分验收通过，整阶段尚未通过**。正式手机 Debug 构建及新增真机用例已运行；手机 lint 7 项失败、同设备前后性能和部分 UI 场景未完成。完整跨端记录见同工作区 AllDayRecording/doc/SYNC_PHASE1_ACCEPTANCE.md；真机准备见 AllDayRecording/tools/quality/sync-phase1-device/README.md。

## 版本与差异

两个仓库均保持 master，无 checkout/提交/推送：
- 本仓库起止 HEAD：04e2e7f994025c1df0e2f653dd83404c9e11582a；初始干净。
- 手机起止 HEAD：fe872618fe8ecd7d205dfe236767c91640985768；初始有用户未跟踪 tests/，其原样例保留。
- 检查两仓库及祖先路径未发现适用 AGENTS.md；当前 HEAD 不包含本轮未提交差异，以 git diff/status 为准。

本仓库修改：
- src/allday_asr/v3/interfaces/transfer/composition.py：从监听线程分离 TLS 握手，最多 32 个连接工作线程、5 秒握手超时、超限关闭、服务关闭释放。等待网络时不持共享锁，保留原认证、CA、证书重载能力。
- src/allday_asr/v3/interfaces/transfer/http_handler.py：26 字符服务端请求编号，白名单校验客户端关联编号、脱敏路由、HTTP 状态与耗时；500 内部记录异常类型/源码位置的原因链，不输出原异常消息/局部变量/私密路径，兼容既有错误字段。
- tests/test_transfer_tls_admission.py：真实 TCP/TLS 的阻塞、释放、上限/关闭、500 安全诊断四项回归。
- tools/sync_phase1_test_receiver.py：本轮手机 UI 用的独立 loopback 慢 TLS 合成服务，只接受固定大小零字节，不启用生产管理接口。

手机实现：结构化连接错误、仅明确不可达时重发现、发现取消与资源释放；定向标注 SQL 与 schema 11 索引迁移；提交后局部应用；短事务串行化/旧 revision 与 snapshot generation 保护；同步/保存/音频/审核对象状态拆分；异步上传文件 I/O 与进度限频；真实控件 ID 和新增 Hypium 用例。完整文件清单位于手机验收记录。

## 根因与边界

真实 socket 测试证实原 SSL 监听 socket 会在接入时等待握手：一个仅连 TCP 的客户端可以使另一个正常 HTTPS 客户端握手超时。修改前该测试失败（约 2.44 秒）；修改后正常 HTTPS 在测试 1 秒截止内返回，重复超时连接释放及 shutdown 验证通过。测试将握手超时缩短到 0.4 秒，生产默认 5 秒。此风险不能解释所有“12 秒未发现”、HTTP 400/500 或音频等待。

保存地址的各种失败此前被吞掉进入发现；手机现在保留 400/401/403/500、TLS/身份、原生错误与可能已发送语义。SDK 通用超时 2300028 无法确定是否发出，保守视为响应不确定，不触发普遍重发现/审核重发。未清除信任、绕过 CA/HUKS 或复用 challenge。

正常标注保存不等待电脑或全量刷新；定向查询返回量不随无关历史转写增加。UI 仍浅复制 sessions 数组，生命周期全快照与备份清单扫描仍有成本；本轮没有完成全部热路径性能保证或调度依赖重排。

## 环境、实际命令与报告

本机 Windows 10.0.26200 x64；项目 .venv Python 3.12.10，pytest 8.4.2，ruff 0.16.5。

在本仓库运行：
```powershell
.venv\Scripts\python.exe -m pytest tests/test_transfer_tls_admission.py -q
.venv\Scripts\python.exe -m pytest tests/test_transfer.py tests/test_device_auth.py tests/test_v3_device_sync.py tests/test_v3_device_annotations.py tests/test_v3_device_reviews.py tests/test_review_audio_guards.py tests/test_review_audio_boundaries.py tests/test_phone_voice_review_flow.py tests/test_transfer_tls_admission.py -q --junitxml=outputs/sync-phase1-pytest.xml
.venv\Scripts\python.exe -m ruff check src tests tools/sync_phase1_test_receiver.py
.venv\Scripts\python.exe tools/sync_phase1_test_receiver.py
```

|执行项|结果|证据|
|---|---|---|
|TLS 回归改前|exit 1，正常 HTTPS 握手超时|本轮终端回归失败；未保留伪造的改前成功报告|
|TLS 回归改后|exit 0，4 passed，多次重复|包含 3 次超时释放、32 上限拒绝/关闭、安全 500|
|相关完整 pytest|exit 0，68 passed / 10.87s|outputs/sync-phase1-pytest.log 与 .xml|
|ruff|exit 0，All checks passed|本轮终端|
|独立合成接收端|启动、接收慢 TLS 字节，完成 UI 后已停止|outputs/sync-phase1-test-receiver/（私有，不提交）|

最初受限环境 pytest 出现 WinError 5；重新在允许执行项目测试的环境运行。新 request_id 曾因长度 24 不兼容既有断言，修复 26 后上述 68 项全部重跑通过。没有删除/降低原断言。

手机侧执行（详情与环境变量见手机记录）：
- Studio Node v24.14.1、SDK API26/platform26.0.0/26.0.0.32；bundled Node v24.19.0 执行 SQLite 结构测试。
- run-host-tests.mjs 最终 exit 0，142/142（entry67、phone75）；outputs/sync-phase1-host-final.log。clean 后 SDK 测试目录映射导致一次超时，复制本次新编译 phone/.test/phone 至 default 并重跑，runner 校验最新报告与用例数。
- 九个 Node 脚本各 exit0：check-sync-phase1、check-sync-connection、check-sync-discovery、check-receiver-connection、check-review-audio、check-voice-review-flow、check-annotation-experience、check-phase2-sync、check-sync-byte-limit；日志 outputs/sync-phase1-check-*.log。
- run-code-linter.mjs exit1，7项 @performance/avoid-overusing-custom-component-check；AnnotationPage、HomePage、MorePage、PeoplePage、SessionInfoTabs 组件。未屏蔽。outputs/sync-phase1-lint-final.log。
- hvigor --mode project -p product=phone -p buildMode=debug clean assembleApp --no-daemon exit0，正式 Debug 应用成功，15.480s；outputs/sync-phase1-build-final.log。构建/静态检查的 ENOENT/EPERM 环境限制在允许环境重跑；中间 ArkTS 编译问题已修复。
- 正式 phone/build/phone/outputs/default/AllDayRecording-phone.hap 已 install -r 恢复；未卸载、不操作 Watch。
- 原样例 DevEco Testing Python main.py，exit0，1/1；新增 run_phase1.py exit0，2/2。下节为本轮真实报告，非宿主测试替代。

## 真机证据与状态

设备 PLR-AL00 手机，软件 7.0.0.109(SP6C00E105R7P3)；原配置选定设备，bundle AllDayRecording.huawei.com / phone / EntryAbility；DevEco Testing Python3.12.10、Hypium/xDevice6.1.0.210、hdc3.2.0c、uitest7.0.0.1 / agent1.2.3。继续使用用户现有 tests/ui/main.py 和 config/user_config.xml，没有 Appium/坐标框架。

- A 原样例通过 1/1：手机 tests/ui/reports/2026-09-26-10-43-10/summary_report.html。
- B 离线保存隔离真机通过：真实生产标注页面/VM/usecase/RDB，远端端口置离线；持久化操作读回1，重启仍1及待同步；隔离数据库故障无第二条/假成功。8秒是断言截止值，不是测量耗时。
- C 部分通过：真实 8MiB 慢 TLS 传输未结束时切页、保存、返回读回、原生播放本地合成音频。接收字节从 8192→保存后458752→播放后884736，全部 ended=0。备份端口替代为合成网络，未宣称完整生产备份链路/滚动/无掉帧已测。
- B/C 最终报告：手机 tests/ui/reports/2026-09-26-11-22-01/summary_report.html（2/2），outputs/sync-phase1-ui-final.log 与 sync-phase1-interaction-evidence.json。代表截图 details/PhoneSyncInteraction/ 时间20260926112239055710，显示合成传输与持久化1；在播放步骤前，播放成功由断言和字节计数支持。原始报告不提交。
- D 客户端边界测试覆盖 HTTP400/401/403/500、5种原生故障、重发现1/0、发现超时/取消/替换/启动失败；真机仅离线代表情形，其余网络故障UI未执行。
- E 真实TLS通过，见上述四项。
- F 存储/VM交错、重复点击、旧版本/快照、回滚重启及既有审核证据/完整播放/不确定提交回归通过；保存中退出的全部真机时序未执行。
- G 未完成同设备改前/改后点击至提交/可见完成分位数，也无帧报告。不能宣称“不卡顿”。

仅宿主后测（HOST_SQLITE_NOT_DEVICE，8次/规模，毫秒）：历史0/1000/10000时 P50=2.3747/2.6047/2.3986，P95=max=3.0635/3.8781/4.2996；每规模8次共16次查询、36返回行，扫描0、保存网络调用0。不是触摸驱动计时，没有设备前后对照，未覆盖大量无关会话的渲染。

## 数据保护、恢复与遗留

两次只读导出手机 phone files、旧 entry files、phone database，每次1892文件/3,614,604,870字节，SHA-256全相同。只在独立 sync-phase1-isolated-20260926.db 写合成数据，使用独立测试CA/正弦音频，没有改真实配对或审核判断。清理精确识别的测试DB/附属文件与测试cache、恢复生产入口、去除rawfile夹具后 clean 正式构建并 install -r。恢复后同三目录再次校验零差异。正式应用未自动启动；不宣称真实库迁移已发生。测试接收端与专用19099反向映射已清理。

schema10→11非破坏回填索引测试通过。旧schema10应用不能读取11；回退须保留新库及未同步操作并使用兼容修复版本，不能清库/覆盖旧备份。真实音频、数据库、密钥、签名材料、HAP和未脱敏日志/报告仅保存在被忽略路径，未提交。

未通过：手机lint7项。未执行完整要求：真机G性能、C滚动/完整生产备份交互、D原生故障UI矩阵、F更多退出时序。已解决的环境阻塞：受限子进程/文件权限及SDK测试目录映射；没有把受阻运行算作通过。剩余同步取消以批次边界为主，不保证全部网络请求即时中断。

下一阶段仅记录：备份/元数据调度、静默同步、审核音频双端缓存与预取；本轮没有实施定时完整 synchronize()、通用任务平台或协议重写。
