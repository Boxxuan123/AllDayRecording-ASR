# B/C/D 同步优化：统一集成验收（2026-09-26）

## 结论与版本

B/C/D 连续实现后统一执行 E。最终候选的四条核心真机流程 T1–T4 全部通过；快速层、构建、宿主测试与静态门禁通过。性能专项为部分完成：有点击到 AVPlayer 开始、本地事务耗时和真实并行顺序，没有实际声学延迟、帧率或主线程阻塞时长测量。不能据此声称所有设备和网络环境均已验收。

| 项目 | 版本 |
| --- | --- |
| 手机本轮基线 | `283f7696a9883a504159275608eceea882fcd7c0` |
| 电脑本轮基线 | `9516a432d0a8f65d748013b29c7de5154efea1c3` |
| 手机最终业务被测源码 | `76a0604c86239c5e220990e38e05b437fa800d96` |
| 电脑最终业务被测源码 | `a7b6343f9f5cbe231f7636afda9f1f7f7d81b0ae` |
| 实际安装、完成 T1–T4 的隔离测试 HAP SHA-256 | `fe466770c012886a387533993dd1c984b08b1cb78a48e3ec69cd664f486186ae` |
| 恢复正式入口并实际覆盖安装的 HAP SHA-256 | `b32cf8434085ce1fa7902149cb10ee00b6f89c732b440d446e9decb764fef92d` |

隔离测试构建由 `build-sync-device.ps1 -Mode bcd` 临时换入已提交的测试入口，使用生产缓存、调度、存储、TLS/HUKS、备份与播放器类；恢复构建使用同一业务源码的 `-Mode production`。正式入口 HAP 检查不含测试配置、测试 CA 或 NativeRefresh 文件，覆盖安装成功后未启动正式业务同步。最后的验收文档、清理用例和审核包导出选择范围属于非业务差异；最终交付提交以文档所在 Git 提交及审核包 manifest 为准，不在本文嵌入自身 hash。

## 范围盘点与实现映射

| 要求 | 盘点及本轮处理 | 主要位置 / 证据 |
| --- | --- | --- |
| B1 内容身份与试听资格 | 原有 review/audition 校验复用；补稳定内容身份及本地描述 | PC `interfaces/device_reviews.py`、`contracts/v3/schemas/review-audio-cache.schema.json`；Phone UseCases / validation |
| B2 手机持久缓存 | 补齐私有持久缓存、工作线程、原子发布、租约、容量和损坏恢复 | `PhoneV3ReviewAudioCache.ets`、`PhoneV3ReviewAudioWorker.ets`；T1/T2、check-review-cache |
| B3 电脑制作缓存 | 补齐持久产物、同 key 合并、有界工作队列、当前证据复核；单条请求定向 hydrate | `adapters/audio/review_cache.py`、`interfaces/device_reviews.py`；pytest cache/guards |
| B4 有限预取 | 补当前及后两项、单并发、前台提升、失败退避和代次保护 | Phone ViewModel / VoiceReviewPage；T2/T3、快速层 |
| C1/C3/C4 | 已有本地 outbox、两路协调、前台恢复、依赖和幂等、失败分类，直接复用 | `PhoneSyncCoordinator.ets`、UseCases、repository；T3/T4、phase2a/recovery/TLS 回归 |
| C2 电脑新结果 | 复用已有前台增量探测，审核页纳入活跃 15 秒节奏 | ViewModel `setReviewAudioWindow`；T4 无本地新操作/无手动刷新自动到达 |
| D1/D2 | 复用本地优先、短事务、刷新保护与局部操作状态；补缓存状态和离线证据提示 | ViewModel / VoiceReviewPage；真实标注页 T3、快速层刷新交错 |
| D3 | Base64/SHA/缓存 IO 真实 taskpool；避免单音频全量 hydrate、每点击扫描目录 | T1 工作线程 tid 与 UI tid 不同；快速层生命周期和热路径 |
| D4 | 保留信任与传输完整性；3.7.0/3.7.1 兼容、无描述走旧受保护路径 | 合同 3.7.1、projection 4；TLS/权限/完整性测试 |

未增加后台常驻权限、离线审核决定平台、模型功能、Watch 传输重写或新的认证体系。故障注入仅隔离接收端读取本地控制文件，不提供生产未认证控制接口。

## 内容身份、预算、调度和状态

电脑内容 key 覆盖按序裁剪/拼接窗口、不可变内容地址及源副本 size/mtime_ns、明确的 PCM/响度处理配置与渲染版本；review key 仍描述当前审核证据，audition key 管理样本完整试听。服务器在命中/制作前验证当前资格，制作后再次复核证据及源内容身份。缓存字节不能直接授予审核资格。

手机由本地受信 receiver、CA 指纹、device、RP、签名 key alias 计算命名空间，不使用 IP 或人物名。命中前只读取本地配置与投影，无需先连电脑。复用内容时重新绑定当前样本元数据，不能继承别的样本已听状态。离线只代表本机已知版本，提交仍需电脑核对。

| 限制 | 默认值与行为 |
| --- | --- |
| 手机缓存 | 单项 16 MiB、总音频 64 MiB、48 项；最多 4 个共享任务、1 个活动准备任务；索引最多 256 KiB |
| 手机发布与清理 | taskpool 内校验长度/SHA、临时文件 fsync/改名；播放租约和短持有期保护；启动有界残留清理；损坏且仍占用的文件不删除；空间不足失败/预取退避，不删录音 |
| 手机预取 | 当前及后两项，串行；当前点击提升排队项并合并已有请求；失败静默退避 60 秒；离页/后台/stop 停止后续排队，已运行的共享任务受控结束 |
| 电脑缓存 | 2 个制作线程、最多 16 个活动及等待任务；单项 16 MiB、64 项、稳态 128 MiB，另有有界在途/临时开销；启动扫描最多 256 项 |
| 电脑制作 | 前台高于待执行预取；短锁不等待裁剪/长 IO；单次 FFmpeg 裁剪超时 30 秒；等待者超时 120 秒不取消他人共享制作 |
| 统一前台同步 | 已有单协调器管理轻量/备份两路；需求合并、最小调度延迟 250 ms；活跃/待处理/备份/审核页 15 秒，普通空闲 120 秒 |
| 可恢复失败 | 带抖动指数退避，上限 300 秒；401/403、身份/TLS及确定非法请求阻止无效自动重试；手动入口唤醒原协调器，不另起链路 |

缓存可用、准备中、正在播放、完整听完、决定确认分开管理；准备/预取不设已听。页面代次防旧下载/播放器回调更新新对象，也防旧页面退出误停新播放。本地保存、电脑回执、备份完成和电脑处理完成沿用独立状态。原始录音删除规则、SHA/续传/manifest 顺序保持不变。

## 实际执行的测试与工具

环境：Windows 10.0.26200 x64；DevEco Studio SDK 26.0.0.32 / API 26，Hvigor 6.26.2，Studio Node 24.14.1；快速层 Node 24.19（SQLite 能力）；PC Python 3.12.10、pytest 8.4.2、ruff 0.16.5；原生 UI 使用 DevEco Testing 自带 Python、Hypium/xDevice/HDC。设备为 PLR-AL00，HarmonyOS 7.0.0.109(SP6C00E105R7P3)，phone product，bundle `AllDayRecording.huawei.com` / EntryAbility。未安装 watch product。

下表路径均为对应仓库的本轮本机私有输出，不将原始报告提交到仓库或审核 ZIP。

| 实际命令 / 范围 | 最终结果 | 耗时 / 报告 |
| --- | --- | --- |
| `tools/quality/build-sync-device.ps1 -Mode bcd` | exit 0，最终候选测试 HAP 构建 | 10.596 s；Phone `outputs/sync-bcd-delivery-build.log` |
| `node tools/quality/run-host-tests.mjs` | exit 0，142/142（Phone 75 + Watch 67） | 最终源码重跑 24.728 s；`outputs/sync-bcd-host-delivery.log` |
| `node tools/quality/run-code-linter.mjs` | exit 0，0 defects，实际 SDK Linter | `outputs/sync-bcd-lint-delivery.log` |
| `node tools/quality/check-<name>.cjs`，下列 14 组 | 每组 exit 0；运行生产类、受控原生适配，不只匹配源码字符串 | `outputs/sync-bcd-delivery-<name>.log` |
| PC `.venv/Scripts/python -m pytest`，下列 13 文件 | exit 0，88 passed | 13.58 s；`outputs/sync-bcd-pytest-delivery.xml` / `.log` |
| PC `.venv/Scripts/ruff check src tests/test_review_audio_cache.py tools/sync_bcd_fixtures.py tools/sync_phase2a_device_receiver.py tools/export_sync_bcd_review.py` | exit 0，All checks passed | 实际本轮执行 |
| DevEco Testing Python `run_bcd.py`（cwd `tests/ui`） | 严格报告检查 exit 0；1 个集成用例内 T1–T4 全通过 | 用例 223.088 s；含报告 228 s；`tests/ui/reports/2026-09-26-20-25-56/summary_report.xml` |
| 清理专用 UI `run -l PhoneSyncBCDCleanup` | fresh XML tests=1，errors/failures=0 | 5.919 s；`tests/ui/reports/2026-09-26-20-31-19/summary_report.xml`；不是额外业务验收数 |
| `build-sync-device.ps1 -Mode production`，HDC `install -r` | 构建 exit 0，明确 install bundle successfully | 构建 12.430 s，覆盖安装 690 ms；`outputs/sync-bcd-production-build.log` |

14 组名称：`review-cache`、`sync-phase1`、`phase2-sync`、`sync-coordinator`、`sync-phase2a`、`sync-phase2a-lifecycle`、`sync-byte-limit`、`review-audio`、`voice-review-flow`、`annotation-experience`、`receiver-connection`、`sync-connection`、`sync-discovery`、`upload-offset`。TypeScript 使用 Studio ets-loader，SQLite 组使用支持该能力的 Node。涵盖持久命中/命名空间、合并/前台提升、缺失/损坏/半文件/容量/租约、资格与旧回调、短事务/回执/依赖/刷新交错、停止与连续触发、发现/400/鉴权/5xx/响应丢失、共享连接及旧接口兼容。原生 API 在快速层被适配，不能把快速层视为真机。

PC pytest 文件：`test_review_audio_cache.py`、`test_transfer.py`、`test_device_auth.py`、`test_v3_device_sync.py`、`test_v3_device_annotations.py`、`test_v3_device_reviews.py`、`test_review_audio_guards.py`、`test_review_audio_boundaries.py`、`test_phone_voice_review_flow.py`、`test_transfer_tls_admission.py`、`test_sync_phase2a_recovery.py`、`test_v3_contracts.py`、`test_v3_bootstrap.py`（均在 tests/）。命令附 `-q --junitxml=outputs/sync-bcd-pytest-delivery.xml`。包含服务重启缓存复用、合并/队列优先、真实音频制作/当前资格校验、传输与认证。

## 四条真机主流程

实际电脑接收端：`python tools/sync_phase2a_device_receiver.py --bcd --public-key outputs/sync-bcd-private/public.txt --output outputs/sync-bcd-delivery`，HDC `rport tcp:19100 tcp:19100` 转发。TLS/CA、HUKS 签名、一次性 challenge、生产 gateway/RDB/备份路径实际运行。独立测试公钥在电脑本机引导注册；没有冒充测试完整 Passkey 注册或真实局域网发现。

| 流程 | 最终实际证据 |
| --- | --- |
| T1 | 两端冷缓存首播；重复两次、真实审核页返回、应用重启、电脑不可达均可手机命中；只删手机测试缓存后电脑命中；缓存 IO 线程 tid 不同于 UI tid；预取不设已听 |
| T2 | 电脑合法业务修改证据后自动到手机，已听资格清除；发送半个 HTTP 音频响应后不播放；慢下载中切换对象，不启动旧样本或授予旧资格 |
| T3 | 生产备份上传合成 WAV 8,388,652 字节；上传中试听、预取、真实标注页保存和滚动；本地持久回执先于上传结束；下一项缓存命中；随后录音与 manifest 完整确认 |
| T4 | 电脑不可达时三次本地保存，应用重启仍有三项；同受信电脑恢复并注入一次 500 后无需点击同步即收敛为 0；电脑通过 classify/assign/sample worker 合法生成新审核样本，手机无需新本地操作或手动刷新即收到 |

只读核对电脑数据库：4 个不同 operation_id，4 条 applied，目标合成 utterance revision=6；没有重复业务行。T3 回执发生时仅上传 786,432 字节且 complete=false。最终 recording 和 manifest 上传记录均 completed，实际文件大小、offset、SHA 与记录一致；manifest 在录音确认后到达。合成录音 SHA-256 为 `b72600c472f945084bded9ae2bc35112c900b461b9ceca19403320846869026f`，manifest SHA-256 为 `582c8bcd9a0cfb8f8f23310dba46cdd7090c6f5062470dfd52f4a0c2bb1cf797`。manifest 中 Watch 标签是合成格式兼容字段，本轮没有从实体 Watch 传输。

原生原始证据只留 PC `outputs/sync-bcd-delivery/native-results.json`、`evidence.json` 等私有目录；不公开原始指纹、人物/转写、路径或凭证。数值模型 provider 使用合成固定向量，真实审核样本仍由生产业务处理生成；本轮未测试大模型推理质量。

## 性能：代理指标、小样本和边界

| 指标 | 本轮实测 |
| --- | --- |
| 点击→AVPlayer onStarted：两端冷缓存 | n=1，507 ms |
| 点击→AVPlayer onStarted：仅电脑命中 | n=1，283 ms |
| 点击→AVPlayer onStarted：手机命中 | n=5，73 / 69 / 68 / 108 / 57 ms；中位数 69、最大 108 ms |
| 真实备份期间缓存播放 / 下一项播放 | 各 n=1，65 / 74 ms |
| 离线空闲本地事务开始→持久提交 | n=3，4 / 5 / 4 ms；中位数 4、最大 5 ms |
| 真实备份期间本地事务开始→提交 | n=1，8 ms |
| 备份期间保存→可见反馈代理 | n=1，313.4 ms；包含驱动点击与条件观察开销，不当作纯应用渲染耗时 |
| 初次电脑音频准备调用 | n=3，约 162 / 173 / 165 ms（包含服务当前资格校验与制作）；命中不重复制作 |
| 整条流程音频接口 | 12 次响应准备，3 miss、9 hit；每份音频 160,078 字节；包含故障注入后的重试及预取 |

12 次记录不是 12 次完整网络传输：其中一次故意截断；JSON/Base64/TLS 开销未测，不将音频字节和当作线上字节。T1 同 key 手机复播不增加音频请求；快速层还断言命中时不调用受信连接/下载。独立后台状态/认证请求不归因于播放，未给出全程认证零请求的错误结论。预取与同样本点击共享任务由快速层行为断言覆盖。

音频代理来自真实按钮处理到真实 AVPlayer onStarted，不是实际扬声器声学测量；未采集帧率、UI 卡顿持续时间、内存峰值或精确磁盘峰值，没有声称滚动成功就等于不卡。空闲保存到可见完成未独立测量；测试 HAP 安装和夹具准备未单独留可靠计时，不事后补算。没有历史版本回装基准或 p99 结论。

## 曾失败的检查与修复

- ArkTS 初次编译对构造参数字段、任意 throw 和对象 spread 报错，改为支持的写法；最终构建通过，未关闭门禁。
- PC 首轮 85 passed / 3 failed：源版本校验需要真实合成文件，旧夹具不存在该文件；canonical 合同回执版本也需更新。补齐夹具/合同后相关 16 项及最终 88 项通过。
- 手机早期宿主 73/75：旧 3.7.0 接收端 mock 被新版本拒绝。修复生产兼容判断，最终 142/142，未仅放宽测试。
- 原生报告 `2026-09-26-20-03-49` 首播失败：初始化空串调用原生摘要导致 build context fail。生命周期无需空摘要，修复并加入拒绝空输入的 mock 回归。
- 原生报告 `2026-09-26-20-07-39` 切页返回失败：旧页退出误停新播放。新增播放代次所有权校验。
- 原生报告 `2026-09-26-20-12-14` T1/T2 已通过、T3 失败：隔离接收端覆写响应函数遗漏 headers 参数，破坏上传响应；修复测试适配转发。
- `2026-09-26-20-19-01` 四流程首次完整通过。随后补同内容换样本元数据绑定、占用损坏缓存保护，重新构建安装最终 HAP，执行 `20-25-56` 四流程、14 快速组、142 宿主和 Linter；最终报告不是沿用修复前结果。
- 恢复正式 HAP 的第一次 HDC 安装因斜杠形式绝对路径识别失败、未安装；改 Windows 路径后明确安装成功。

最终所列执行范围无剩余失败。范围外及未执行项：真实 LAN/热点发现真机矩阵、Passkey 初始注册仪式、实体 Watch 传输、真实模型推理、进程被系统长期挂起后的常驻同步、完整离线审核决定、帧/声学与长期大样本性能。这些不能计入本轮通过数。

## 数据保护、恢复、兼容与交付

测试前先停止应用，只读备份当前 phone files、历史 entry files 和 phone 数据库；测试后清理仅本轮 `sync-bcd-*` 数据库/缓存及 `sync_bcd_isolated_settings`，通过专用 UI 删除本轮 HUKS key，恢复正式入口 HAP 后再次只读导出。逐文件 SHA-256 比较：前后均 1,892 文件、3,615,080,658 字节，different_files=0，脚本 exit 0。当前 phone files 1,861 文件/3,553,876,341 字节，历史 entry 26 文件/28,961,142 字节，数据库 5 文件/32,243,175 字节。测试后导出耗时 151.528 秒；测试前分项 145.959 / 1.329 / 1.247 秒。两次备份与文件名清单仅存 PC `outputs/sync-bcd-private/before`、`after`，不公开。

未卸载应用、未清正式库、未重置正式配对、未删除真实录音或操作真实审核决定。清理后设备数据库目录仅原有 5 个 `phone_v3_projection.db*` 文件，偏好仅原有 `computer_transfer_v1` 及 lock；原有缓存目录保留。本轮独立接收端已停止，专用 HDC rport 已移除。正式数据保留核对通过。

本地缓存是派生数据，旧条目无新字段继续原受保护路径，保留本地数据库、不清库升级；合同为兼容补丁 3.7.1，projection 4 不变。回退代码需保留原业务数据库和录音；允许删除明确的派生缓存后按需重建，不建议回装旧版本覆盖已变化业务状态。未实际对用户正式库执行回退演练。

最初五项体验的结论：连接错误分类/TLS/重试复用且快速回归通过，但未完成真实局域网矩阵；重复试听由双端持久缓存和有限预取解决并有真机证据；同步不锁住本地保存/试听的并行事实已验证，但帧性能未测；标注先本地提交和离线重启已验证；前台自动恢复及远端新结果自动到达已验证，系统挂起场景不作常驻承诺。

交付使用正常 commit 与 `git push origin HEAD:master`，不 force。最终回复分别给出 `git rev-parse HEAD` 与 `git ls-remote origin refs/heads/master` 相同的完整值、固定版本 commit/本文在线链接。若有任何推送失败必须单独报告，不以本地完成替代。

审核包由 PC `tools/export_sync_bcd_review.py` 从两端最终已提交 HEAD 读取所选源码、测试、合成夹具生成脚本、合同、本文、架构与历史参考文档，包含本轮基线→HEAD diff、repo/版本/文件 SHA-256 清单，并回读核验全部文件。目标为 PC `outputs/sync-bcd-final-review.zip`，仅本机保留，不提交。排除运行时 outputs、真实数据、数据库、音频、转写、密钥/完整证书、HAP 和原始报告；包含删除行的 diff 也检查。最终 ZIP SHA-256 与完整提交见最终交付回复及包内 manifest。


## 2026-09-27：F1–F4 定向修补与本轮验收

本节是新一轮证据；上文 2026-09-26 的通过数、HAP 与数据核对均只属于历史，不充当本轮结果。本轮从手机 `dd9bec56c2d8478bb576452abe110ec6b01ec9e7`、电脑 `5e00912f2424532edb88405925c57ad455ce80d4` 开始，两端起始 clean master、fetch 后与 origin/master 相同。附件 `bcd-audit-evidence.zip` SHA-256 实测为 `abd360b55afd05b578c131501cb429570d95ddde0a4cf7e42b1157a7db620c71`，与说明一致。阅读其复现条件和摘录后，把“缺陷出现”的断言改为仓库中的正确性断言，没有用原复现脚本退出成功当修复通过。

| 项目 | 原行为 / 根因 | 修补与正确性断言 |
| --- | --- | --- |
| F3 电脑 worker | finally 中淘汰改名的 PermissionError 逃出工作循环，池可耗尽 | `review_cache.py` 将清理 OSError 与制作/发布失败分离；保留未删除 entry/trash 字节，制作前保守预留最大单项空间及条目，无法回收则及时拒绝；单清理通道、1 秒冷却、60 秒限频诊断且不打印敏感路径；不可用工作池及时失败。故障持续时两个线程存活、落盘与队列有界，解除后成功，旧命中可用；发布失败重试、关闭排队 future 和活动任务资源归属分别断言 |
| F2 手机陈旧索引 | 满 48 项时另一个旧文件已消失，unlink 中断索引修复，连续新 key 均被阻塞 | Worker 通过导入的 `PhoneV3ReviewAudioFiles.ets` 使用 SDK 数值 13900002 区分 ENOENT；每次成功删除/确认缺失后立即原子写索引，再执行后续淘汰/写入。48 项与可控时钟测试连续两个新 key、重建对象、修复后新写失败仍持久；权限错误不虚增空间，既有租约回归保留 |
| F4 同窗口预取 | identity 一致即返回，失败没有可到期调度的需求 | ViewModel 最多 3 个 ready/running/waiting/blocked 需求及一个合并截止定时任务；复用已有前台/网络恢复回调；临时错误 60/120/240/300 秒退避，早恢复也保留到期检查。缓存退避先允许有效命中/共享在途。用虚拟时钟证明早恢复后无事件仍成功、晚恢复、ready/inflight 去重、离页/后台/stop 取消、授权错误不盲重试；预取不播放、不授予已听资格 |
| F1 契约字节 | 附件发现 7 项摘要对应 CRLF 而非已提交 LF；本机实际 Git 系统配置为 core.autocrlf=true，无局部契约 LF 规则 | 两端局部 `.gitattributes` 固定相关 JSON/生成引用 UTF-8 LF；`sync_v3_contracts.py` 先明确将 CRLF 输入改写为 LF，再计算文件摘要→manifest→手机 source/fixture 摘要，无循环或时间戳；bare CR/BOM 报错。校验器对分发原字节严格断言、不偷偷 normalize；重复生成稳定，CRLF 改动后 check 拒绝。协议仍 3.7.1，projection 4，无新增业务语义 |

没有已有持久生成脚本可修订，因此新增上述窄范围契约同步入口；电脑 JSON 是单一来源，不手填某几个摘要、不改全局 Git 配置、不整库 renormalize。SDK 自带 `@ohos.file.fs.d.ts` 明确 13900002 为 No such file or directory；测试适配器用这个数值模拟，而不是依赖英文错误文本。导出脚本复用原实现，基线更新为本轮审核版本，增加导出字节的内层契约核验。

### 本轮候选与实际检查

手机业务候选 `88ce8308f6e9225dd9fcea949a655e9e5253258f`；电脑业务实现 `2dacaeb7dd052b42479eb88fa4e958c4b2295485`，随后 `32937bc21dad53ef963b4102d25a79626b21971e` 仅补发布/关闭测试并执行。收尾资源审查又发现原有零计数 pin 键会残留，电脑 `a47fdd805aa2eace9d8b789cc7c5bb3a334cbadc` 在 read finally 中移除零计数键，加入断言并重跑受影响缓存/真实制作 8 项，exit 0 / 2.03 秒（`outputs/sync-targeted-cache-delivery.xml`、`.log`）。这不是文档-only 修改；LAN 接收端实测仍对应前述 `32937bc`，最后的 pin 状态清理由电脑针对性回归覆盖，没有冒充在新版本重跑过 LAN。最终验收文档及导出清单变更另在交付回复说明。

| 实际执行 | 结果 / 本轮报告 |
| --- | --- |
| PC `python -m pytest tests/test_review_audio_cache.py tests/test_review_audio_guards.py tests/test_review_audio_boundaries.py tests/test_phone_voice_review_flow.py tests/test_v3_contracts.py tests/test_v3_contract_generation.py tests/test_transfer_tls_admission.py tests/test_sync_phase2a_recovery.py -q` | 正常本机权限 exit 0，41 passed / 10.13 s；PC `outputs/sync-targeted-pytest-final.xml`、`.log` |
| 补发布/关闭测试后 `python -m pytest tests/test_review_audio_cache.py -q` | exit 0，8 passed / 1.98 s；`outputs/sync-targeted-cache-final.xml`、`.log`；与前 41 项有重复，不相加冒充唯一测试数 |
| 两端 `git archive HEAD` 完整导出，以导出 src 为 PYTHONPATH、导出 phone 为 ALLDAY_HARMONY_REPO，运行 `tests/test_v3_contracts.py tests/test_v3_contract_generation.py` | exit 0，9 passed / 0.55 s；PC `outputs/sync-targeted-committed/contracts-archive.xml`、`.log`；额外 exact-byte verify 通过，不仅检查外层 ZIP |
| `python tools/sync_v3_contracts.py --phone <phone checkout>` | 生成后原字节校验通过；本轮生成稳定性/CRLF 拒绝断言在 pytest 中执行 |
| 手机 `node tools/quality/check-<name>.cjs` | 9 组每组 exit 0：review-cache、review-prefetch-recovery、review-audio、voice-review-flow、sync-coordinator、sync-phase2a、sync-phase2a-lifecycle、sync-connection、receiver-connection；Phone `outputs/sync-targeted-<name>.log` |
| taskpool helper 模块调整后重跑 check-review-cache | exit 0；Phone `outputs/sync-targeted-cache-final.log` |
| 正式 ArkTS 编译器 `build-sync-device.ps1 -Mode bcd` | exit 0，10.316 s；Phone `outputs/sync-targeted-build-candidate.log` |
| 实际 `run-code-linter.mjs` | exit 0，0 defects；Phone `outputs/sync-targeted-lint.log` |
| PC ruff（修改的生产缓存、契约生成、测试、receiver、导出脚本） | exit 0，All checks passed |

环境沿用 Windows 10.0.26200、Python 3.12.10、pytest 8.4.2、ruff 0.16.5、DevEco SDK 26.0.0.32/API 26、Studio Node 24.14.1、快速层 Node 24.19。没有在 Linux 实机执行测试，不声称 Linux 全套通过，也没有复用旧 88/142 数字。本轮首次 pytest 沙箱运行是 25 passed / 8 failed / 8 errors，失败涉及临时目录/合成夹具文件的 WinError 5 权限阻断，随后同批正常权限 41 passed。首次构建因 ArkTS 任意类型 throw、第二次因 @Concurrent 不允许引用同模块非导入辅助函数失败；保留错误对象显式类型并拆为导入模块后构建通过，没有关闭规则。

### 本轮真机、数据保护与交付

本轮实际安装并执行 LAN 流程的 HAP：SHA-256 `9e91a4ec7bee446cf883e7bca6640e62dc9a822f0a9a6b2d7f6d3979c74e551e`，来源为手机 `b24ea6f16298a5019a79282c4509262c3555550f` 加本机生成的隔离 CA/配置；该提交相对 `88ce830` 仅恢复既有测试资源打包方式，业务代码相同。接收端运行电脑 `32937bc21dad53ef963b4102d25a79626b21971e`。原生续验脚本版本为手机 `9bbf26ce34bcb52cd4c49f90bf52b13f3c18a085`，与 HAP 源码仅差 UI 测试/严格报告入口，无需重装业务包。

实际业务链路为手机 Wi-Fi → 电脑现有 WLAN 私网地址的 19100 端口，启动命令为 `python tools/sync_phase2a_device_receiver.py --bcd --host <本机 WLAN 地址> --public-key <本轮公钥文件> --output outputs/sync-targeted-lan`。接收端记录 peer_loopback=false，TLS、固定 CA、HUKS challenge 签名与生产 gateway/upload 实际执行。HDC 仅设备控制/安装/读取状态，前后 `fport ls` 均 Empty，没有 rport、localhost 或业务隧道。此轮为保存地址直连通过，**未覆盖实际 mDNS 发现**；未修改网络、系统锁屏或防火墙设置，也未改正式配对。

| 真机检查 | 实际结果 |
| --- | --- |
| 同窗口失败→早恢复→自动准备 | 通过。测试接收端仅音频路由返回一次 503；恢复后保持同一窗口、不点击同步或播放，三个需求在真实 60 秒退避后自动 ready；ready 时均未被标记完整听过 |
| 播放、重听、切页返回 | 通过。使用真实 AVPlayer 完整播放，重听不增加该音频请求；切页返回和后续下一项命中断言通过 |
| 真实备份中标注与回执 | 通过前置并行断言。生产路径上传 8,388,652 字节合成 WAV，标注回执时上传仅 262,144 字节、complete=false，手机持久队列已确认；下一项缓存复用和滚动断言通过 |
| 上传完成及完整性 | 首轮 180 秒观察预算超时（失败保留）；相同上传继续，未重建/重传。录音从 03:05:28.885 UTC 至 03:12:23.427 UTC 完成，约 414.542 秒；manifest 于 03:12:26.438 UTC 确认。续验通过，独立只读核对大小、offset、SHA 与 completed 状态一致 |
| 实际断线错误→自动恢复 | 续验通过。使同一受信接收端关闭连接，三次本地提交后必须先观察到真实电脑连接错误，断言不是“12 秒”发现超时；恢复后无手动同步收敛为 0。电脑 client_operations 仅 4 个 applied 唯一操作（并行标注 1 + 恢复 3），无重复业务生效 |

录音 SHA-256 `b72600c472f945084bded9ae2bc35112c900b461b9ceca19403320846869026f`；manifest SHA-256 `582c8bcd9a0cfb8f8f23310dba46cdd7090c6f5062470dfd52f4a0c2bb1cf797`。这里只公开合成内容摘要与聚合事实，不公开原始状态、身份/地址/路径或审核文本。

真实命令（cwd Phone `tests/ui`，环境变量 SYNC_BCD_ROOT 指向本机隔离输出）：DevEco Testing Python `run_targeted.py`，以及只续验受影响部分的 `run_targeted.py --resume`。报告与失败情况如下：

- `2026-09-27-11-01-51/summary_report.xml`：1 failed，配置未进入应用。HDC 普通写入与 Debug 挂载写入被设备权限拒绝；放弃该方式，恢复既有打包隔离资源，未改设备权限。首次启动还曾因锁屏被系统拒绝，用户手动解锁后继续。
- `2026-09-27-11-03-44/summary_report.xml`：1 failed，首轮合并脚本在上传完成的 180 秒观察上限超时；此前预取恢复/播放/并行保存与回执断言已通过，私有 `native-partial-results.json` 保留预取结果。未把此报告记为通过。
- `2026-09-27-11-09-26/summary_report.xml`：续验准备失败，等待的旧提示文字已改变；改为稳定的 `phase2-scroll` 根组件，不重置业务状态。
- `2026-09-27-11-10-35/summary_report.xml`：**1 passed，errors/failures/ignored/unavailable=0，132.398 秒，严格 runner exit 0**。同一上传完成、实际断线错误及自动恢复通过，证据在 PC `outputs/sync-targeted-lan/native-continuation.json`、`evidence.json` 和独立合成数据库；原始报告均仅本机保留。
- `2026-09-27-11-13-23/summary_report.xml`：专用隔离 HUKS 清理 1 passed / 5.678 秒，不计作业务验收。

结论：F1–F4 相关快速/契约门禁通过；LAN 所需行为通过首轮已通过部分加同状态续验完成。**没有声称首轮合并脚本全通过；修改为 600 秒测试观察上限后的整条一体脚本未从头重跑**，只重跑受影响的完成等待与断线恢复。这个上限仅属于测试观察，不更改生产统一超时、鉴权或重试策略。

正式入口 HAP 由 `build-sync-device.ps1 -Mode production` 在移除隔离资源后构建，exit 0 / 12.042 秒；SHA-256 `b072182f5173e5d6ebfd6054fefcf87f95cdeeadcd67034b3e4138748469fa31`，已 `install -r` 覆盖安装成功，未启动正式同步。ZIP 检查无隔离配置/CA/NativeRefresh；本轮测试数据库、合成缓存、HUKS key 均清理，正式偏好仅原有条目；独立 LAN receiver 已停止。

本轮重新执行数据保护，不沿用旧结果：测试前导出耗时 174.556 / 1.699 / 1.566 秒，测试后导出总计 160.329 秒；对 PC `outputs/sync-targeted-private/before` 与 `after` 运行 `verify-phase1-exports.py`，exit 0，前后均 1,892 文件、3,615,080,658 字节，different_files=0。两个新目录的逐文件 SHA-256 清单仅本机保存；即使统计与上一轮相同，也是这次重新导出和计算的结果。没有卸载应用、清正式库、删除真实录音或重置正式配对。

未执行/边界：真实 mDNS 发现、热点切换矩阵、Passkey 初次注册、实体 Watch 传输、真实模型推理、长时压力、帧率/声学延迟、Linux 实机全套测试；本轮没有这些通过结论。最初测试权限/配置/编译/观察预算和选择器失败均已分别列出，不能混作业务通过。

新审核包目标为 PC `outputs/sync-bcd-targeted-fixes-review.zip`，从最终已提交 HEAD 导出，基线为本节起始两个审核提交；包含源码、直接依赖、正确性测试、合成夹具生成代码、两端本文、diff、版本及逐文件 SHA。外层文件清单及内层契约摘要都校验，不改写导出的 Git 字节。包只留本机，不含数据库、真实录音/转写、凭证、HAP、原始私有报告或 .git。


## 2026-09-27 标注同步锁与确认延迟联合修复

见 [本轮验收说明](annotation-sync-latency-fix.md)。源码和本机回归已交付；真机 LAN、T0→T6预算及本轮HAP安装未验收，不能用历史结论替代。
