# 阶段二 A 收尾（电脑端）

2026-09-26。**关键正确性回归通过；完整阶段验收部分通过。** 本轮重新执行的证据单独记录，上一轮 [验收文档](sync-phase2a-acceptance.md) 保留为历史记录。完整最终 SHA、固定版本文档链接和远端核对结果在交付回复给出；文档所在提交就是可审查交付版本。

## 版本、改动与真实测试

开始 master 干净，HEAD/远端均为 `95f3ff16a4edffe4fa2681b3ecd4b8d62a6f26af`。接收端合成故障夹具提交为 `61c7a91f25e792a4dfb784b6154084cf3d3eb12b`；生产服务端源码未改变。只在本地独立 outputs 目录的 fault.json 控制测试 handler 断连、500-once、提交后 drop-once，不新增网络控制接口。正式 CA、HUKS challenge/signature、DeviceGateway、SQLite、分片 offset/SHA-256/manifest 校验不绕过。

本轮执行（Python3.12.10/pytest8.4.2/ruff0.16.5，Windows10.0.26200）：

```powershell
.venv\Scripts\python.exe -m pytest tests/test_transfer.py tests/test_device_auth.py tests/test_v3_device_sync.py tests/test_v3_device_annotations.py tests/test_v3_device_reviews.py tests/test_review_audio_guards.py tests/test_review_audio_boundaries.py tests/test_phone_voice_review_flow.py tests/test_transfer_tls_admission.py tests/test_sync_phase2a_recovery.py -q --junitxml=outputs/sync-phase2a-closeout-pytest-final.xml
.venv\Scripts\python.exe -m ruff check src tests tools/sync_phase1_test_receiver.py tools/sync_phase2a_device_receiver.py tools/export_sync_phase2a_review.py
```

**70 passed，11.21s，exit0；ruff 通过，exit0。** 首次普通沙箱运行为61 passed/7 failed/2 errors，涉及测试临时目录和子进程 WinError5；取得所需本地测试权限后重跑通过，没有修改业务断言。电脑生产源码在这些测试之后没有改动；新增夹具另在真机链路执行，导出工具另有实际 ZIP 导出及逐文件哈希复核。

手机修复先有失败回归：最后一次同步增量在本地保存期间落库并被延后显示，随后断网导致 UI 等待下一次成功网络。现保存完成/恢复前台处理已有本地 dirty 投影，以单读取者和 generation 保护避免旧快照覆盖。正常定向刷新和短事务保持。三个页面链接已有 Observed ViewModel，lint规则未放宽。

手机本轮142/142宿主（23.468s），13个检查脚本各exit0，实际lint为0缺陷。新增400/403/TLS/identity终止自动重试、500/timeout退避和100触发合并行为矩阵通过。这些是分层测试，不冒充原生全部故障矩阵。

## 原生验收与限制

手机候选生产代码 `c98fac77566105665c5db62099c849310441ffc1`；最终被测夹具提交 `f6864926d54fef2b9794a375c968acac0507bf52`，生产修复代码未再改变。实际安装测试HAP SHA-256为 `dc35dff8bf02a2f74ded0ae44af278833215ec8129daf5cda4eb476c612d94ca`。恢复正式入口的已安装HAP为 `0d087b2b2eb0edb6584e82745c153b8b3b649cf9c0a2d5151505607d01f12c7e`，phone Debug clean assembleApp 12.818s/exit0，12个ZIP条目且无测试rawfile，未启动真实同步。测试入口使用独立 phone 原生库、独立 HUKS alias、合成 WAV 和本地引导受信设备；不覆盖正常生产录音扫描/筛选、真实 LAN 发现或 Passkey 注册仪式。

失败报告均保留：第一次夹具把“可发送操作”当“完整队列”而数量失败；第二次已通过 A、断网重启自动恢复和响应丢失重放，C 因夹具假设首个会话有片段而失败。分别修复观察方法和按 ID 定位，不改生产依赖语义或删除断言。

第三次C夹具在同一保存对象上变更revision，正确触发过期片段保存拒绝，却被错误断言为保存丢失。改为另一条独立片段上的增量，并另加宿主断言确认过期目标仍拒绝；未降低安全校验。

最终原生报告：手机 `tests/ui/reports/2026-09-26-16-20-39/summary_report.html`，**1/1、失败0**，使用本提交接收端与上述最终HAP。一个完整用例依次执行：生产真实备份中UI本地保存、电脑回执、手机native/UI pending归零、播放/滚动、实际8,388,652字节完整录音与manifest；真实断连时3次有依赖保存、停止/重启、连接恢复后自动收敛；提交后丢响应、同ID恢复；原生库控制刷新交错、最后增量无后续成功网络仍可见、旧快照/停止回调保护和重进读回。C中的网络完成时序由夹具控制，不能称作真实协议测试；回执事务恰逢另一条保存的独立原生场景未单列完成。

第二次实际数据库只读复核：8次 applied 回执事件对应7个唯一 operation_id，client_operations为7；初始1个标注事实加7次业务生效得到8个事实，初始revision2加7得到revision9。丢响应的相同ID获得两次相同应用结果，没有第二份业务效果。最终轮另行核对。

最终轮 outputs/sync-phase2a-closeout-device4/verified-facts.json 再次只读核对上述计数，并断言电脑持久化operation_id集合与全部回执ID集合完全一致：8事件/7操作/8事实/revision9。没有使用上一轮计数代替本轮结果。

手机实测queue入口至本地提交：备份中UI样本1次5ms，备份完成后夹具连续保存3次均5ms；驱动观察触摸到完成约314ms含驱动开销，不是应用耗时统计，不构成同UI性能对照。实际取得10秒hiperf线程计数；未取得帧率或阻塞时长。跨设备时间线、精确时间戳缺口和所有失败轮次HAP对应关系详见手机收尾文档。

未执行或不能声称通过：原生400/401/403/证书完整故障矩阵、真实地址发现/探测超时全时延、原生多数据规模下相同UI保存对照、帧/主线程阻塞统计、可比历史HAP性能对照。宿主数据规模测试不当作真机性能；审核决定未接入自动盲目重放。完整手机步骤及证据边界见手机同版本 `doc/SYNC_PHASE2A_CLOSEOUT.md`。

## 提交和本地脱敏审核包

本轮手机测试前后重新只读导出并比较：各1,892文件、3,614,605,522字节，SHA-256差异0，exit0；不是旧轮次证据复用。正式phone入口覆盖恢复成功，测试密钥/独立库/cache/rawfile与19100映射已清理，没有卸载或清真实数据。验收后生产源码与最终原生夹具输入未变；后续只加文档、宿主安全断言和本地导出工具。完整性能、精确时间埋点及全部故障/交错仍未完成，故整阶段为部分通过，未进入音频缓存/预取。

正常提交后推送 origin/master，无 force。最终以 `git rev-parse HEAD`、`git ls-remote origin refs/heads/master` 与重新 fetch 的提交逐项核对，线上验收链接固定到 SHA。

```powershell
.venv\Scripts\python.exe tools/export_sync_phase2a_review.py --phone '<手机仓库绝对路径>' --output outputs/sync-phase2a-closeout-review.zip
```

导出工具只通过 `git show HEAD:path` 取已提交文本，并从阶段一手机763f95a、电脑895c294生成包含删除行的diff；验证基线祖先关系、扫描PEM实体/凭据、生成各端完整HEAD及每文件SHA-256清单，最后重新打开ZIP验证完整性和每个文件哈希。代码中用于解析PEM的分隔符字符串不是证书实体，预检已区分。

审核包只保存在忽略的 outputs，不能公开提交。运行数据库、音频、真实转写、HAP、密钥、证书、设备配置、原始报告不在导出范围。文档的最终提交之后若仅改交付说明，不要求无穷重测，但必须核对生产和被测夹具输入未变。
