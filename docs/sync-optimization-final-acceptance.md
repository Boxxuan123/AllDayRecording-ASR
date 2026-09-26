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
