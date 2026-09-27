# 标注同步锁与确认延迟修复验收（2026-09-27）

本轮结论：源码修复与本机回归通过；真机 LAN 集成及端到端预算未验收。未停止正式同步，未把电脑本地耗时当作手机 T0→T6。

## 版本

起始 Phone：a25b6cab9a1e7629d9ca9525cb8c7d4ba0942d68。
起始 Desktop：20723f0ecd45b6cd6a6178cf720069f566d5135e。
两端起始工作区干净，与历史审核基线一致。

电脑最终业务源码：22d63dee7a880d648a1dbbadcceb0cdfcce5986b。主体候选 b5fec949006a04992f394a996079ecc3c74ffc59；随后修补“来源投影删除后仍保留跨会话冲突”，重跑受影响36项。
手机 HAP 业务源码：ea86c230dca7d11fbeee52da343e9922cddd4320。d93289574b4967100bc9fa32f08169e4a379ac70 只将宿主阻塞测试强化为人物和声音各一条，执行通过，不改变HAP。
最终提交仅追加本说明与原验收文档入口；完整最终hash以交付回复及审核包manifest为准。

## 调用链与修改

旧电脑链路：普通UOW → BEGIN IMMEDIATE → claim → sample_plans；发布前再次在写事务内完整规划。此前现场线程采样观察到 annotation-samples 扫描JSON时，认证线程等待BEGIN。这是此前现场证据，不是本轮新的19/36秒性能样本。

新链路：短写认领 → 六个SELECT取得WAL一致性快照 → 关闭连接 → 事务外解析/索引/规划 → 事务外模型接口 → 短写校验并原子发布。生产worker发布前不再调用sample_plans。每条会话projection JSON只解析一次；复用fact索引、媒体/维度区间树、track/person和副本索引，保留跨会话共享媒体及来源投影已删除的冲突/撤销事实。窗口规则与key保持确定性。快照预算100,000行/64MiB原始文本；超限明确失败、有界重试，不能悄悄少取数据。

v16增加输入版本表和触发器，覆盖 annotation_facts、annotation_fact_audio、utterances、capture_segments、audio_assets、audio_replicas、speaker_cluster_memberships、person_cluster_links、speaker_tracks、speaker_clusters、persons 的INSERT/UPDATE/DELETE。包含快照之后新增的冲突事实；所有SQL写入口均参与。发布比较输入版本、generation、token、租约有效期及model/version；current选择与job完成原子提交。队列/审计/输出表不推进输入版本。全局版本保守覆盖：无关会话变更也可能导致重算，保留已有重试上限，没有保证持续高写入时样本永不延后。

认证纯读取用显式只读UOW；完整签名验证后仍短写使用记录/审计，并在写事务内重新授权。TLS/CA、HUKS、一次性challenge、请求绑定与撤销保留；默认写事务和busy_timeout未改。BEGIN失败也关闭连接。

人物确认用person_basic主键查询；选择列表用people_choices最小字段。完整统计页面保留原list_people。没有合并操作ID或绕过校验、幂等与依赖。

手机每批applySyncResponse真正提交后立即通知operation IDs、回执状态与刷新范围，已提交批次先退出失败标记候选。通知异常不改变事实。outbox/cursor仍只有一个提交者，审核/人物读取作为同一协调器的有界独立通道，有局部错误与退避。授权/身份/TLS错误共享安全停用。dirty/generation保留交错刷新；成功空增量退休覆盖层也产生刷新。confirmedOperations将已确认待投影与待发送数量分开，保留覆盖层和“电脑已接收，结果更新中”。

## 实际执行

- Desktop集中命令：python -m pytest tests/test_annotation_sync_latency.py tests/test_phase2_samples.py tests/test_phase1_human_facts.py tests/test_v3_device_sync.py tests/test_v3_device_annotations.py tests/test_annotation_purposes.py tests/test_sync_phase2a_recovery.py tests/test_v3_contracts.py tests/test_v3_contract_generation.py tests/test_v3_bootstrap.py -q --disable-warnings。exit 0，85 passed / 10.79s。
- 最终跨会话补充后重跑 latency、phase2_samples、phase1_human_facts：exit 0，36 passed / 7.84s。与85重叠，不相加。
- 真实worker、planner、SQLite：32和1800行，用event/barrier在快照之后挂起规划；完整ECDSA认证/审计和独立声音标注须在释放barrier前、2秒截止内完成。finally释放线程。旧结果不发布current，重算确实产生合成样本。
- 竞态覆盖事实冲突、投影、副本、关联输入、generation、lease token、model；旧结果不发布。与20723f0冻结参考算法比较选择/key一致；跨会话新增声音排除、删除其来源投影后也不能漏检。
- 1800行/3600事实读取六个SELECT；1800份projection JSON各解析一次，事实value/payload各一次。32条人物操作与重放、选择列表禁止调用重统计，断言通过。
- 手机九组Node/真实SQLite宿主检查全部exit 0：check-annotation-sync-latency、check-sync-phase2a-lifecycle、check-sync-phase2a、check-sync-coordinator、check-sync-byte-limit、check-phase2-sync、check-annotation-experience、check-review-prefetch-recovery、check-review-cache。最终人物＋声音阻塞用例再次exit 0。不是手机真机。
- 手机新断言：reviews不放行时两条独立操作先提交且可见；读取随后失败不回退；空增量保持正确；晚到投影退休覆盖层；同一outbox最大并发1。生命周期用例覆盖本地保存/最后增量交错、离线持久化、重进、stale selection。
- Ruff本轮Python修改exit 0；真实Code Linter SDK26/API26 exit 0、0 defects；build-sync-device.ps1 -Mode production exit 0，BUILD SUCCESSFUL 12.214s。
- core_schema_version从15到16，Device API与投影版本不变。沿用sync_v3_contracts.py生成LF摘要；9项契约/生成检查包含于85项。最终Git导出再校验内部契约。

初次失败未计入通过：夹具尝试修改不可变capture segment、签名helper参数和查询计数包含ROLLBACK已修正；宿主新增原生计时适配已补；empty-page断言发现确认计数拆分遗漏，已修复；ArkTS namespace-as-object编译错误已修复重建。没有放宽正确性断言。

私有报告：Desktop outputs/sync-live-diagnosis/ 下 desktop-tests.log、planner-final-tests.log、各check-*.log、phone-build.log、phone-lint.log、ruff.log、benchmark.json/log。不公开原始报告。Python3.12.10、Node24.19.0、Windows、ArkTS API26。

## 性能证据与预算

运行前采用的LAN目标：健康已配对直连单条T0→T6 P95≤2秒；32条全部确认≤5秒。本轮没有实测这两个端到端指标，不能标达标。

手机使用STARTUP单调时钟记录T0本地提交、T1调度完成、T2请求调用、T5回执落库、T6可见通知；电脑用perf_counter记录T4、事务等待/持有和claim/read/plan/publish。未跨端相减壁钟。

python -m tools.benchmark_annotation_sync，最终电脑源码执行exit 0。32/1800行分别64/3600条人物/声音事实、非空投影和真实样本任务；确定性embedding provider，不是实测真实模型。单条每种条件20次，批量每种条件5次；批量不伪报P95。

|规模|旧规划一次写事务内耗时|新一致性读取|关闭连接后的规划|
|---|---:|---:|---:|
|32行|4.21ms|3.98ms|0.95ms|
|1800行|1570.21ms|28.46ms|39.08ms|

同库旧/新规划直接比较；旧发布前还会再规划，但没有把一次测量乘二当作实测总时延。

1800行下电脑MobileSyncService耗时，包含事实/回执提交，不含手机/网络/HUKS/UI：

|操作|数量|样本任务|重复数|中位ms|P95ms|最大ms|SQL等待最大ms|SQL持有最大ms|
|---|---:|---|---:|---:|---:|---:|---:|---:|
|人物|1|空闲|20|13.97|15.16|17.71|0.93|13.07|
|人物|1|活动|20|15.64|29.07|58.92|27.84|15.27|
|人物|32|空闲|5|154.33|—|173.60|0.76|166.18|
|人物|32|活动|5|246.58|—|278.00|171.89|258.71|
|声音|1|空闲|20|19.84|23.04|61.33|1.08|55.20|
|声音|1|活动|20|18.49|70.01|81.56|74.52|67.29|
|声音|32|空闲|5|93.31|—|111.60|0.68|104.16|
|声音|32|活动|5|110.05|—|118.47|132.04|115.45|

SQL最大值是该组连接事务聚合，含正常批量标注，不是planner单独持锁。后台结果68 processed、30因并发变更retryable、2 not_applicable；没有停止/清空worker换速度。仍有约0.26秒批量写事务。旧单条/批量整体和手机端到端未重测；不能说19/36秒已降到表中数字。

## 真机、安装、数据与上线

本轮未安装HAP，未执行DevEco/Hypium短流程、真实Wi-Fi标注确认或发现。既有隔离入口需要同包覆盖安装；当前正式应用正在使用，遵守不中断同步，未替换运行中应用。因此真机LAN、端到端预算和本轮安装仍未验收，宿主不替代真机。

构建候选HAP（signed.app内AllDayRecording-phone.hap）SHA-256：
6c19ec375f1249c256bce54b9f9168a1dcae7de3a1dbc8b6f841a41cddd52545。
仅构建，未安装。本轮没有重新提取核验当前安装包。上一轮最后安装记录为
b072182f5173e5d6ebfd6054fefcf87f95cdeeadcd67034b3e4138748469fa31，
属于历史记录，不能作为本轮版本通过证据。

正式服务未结束/重启；正式SQLite仅只读核对schema=15、journal=WAL，未运行v16迁移。写入、注入和模型替身均在新合成目录；未删除录音、修改真实人物/审核、清库或重置配对。没有导出/回退正式库，没有沿用以前1892文件统计冒充本轮核验。

运行进程不会随源码推送自动升级。当前业务同步结束后仍需正常重启新电脑服务以应用v16，并安全安装新HAP、补短LAN验收；本轮未擅自执行。v16不删除/重写事实，但旧代码会拒绝更新后的schema；回退需保留迁移前备份或使用兼容修复，不能回退正式库测性能。

未测：真实模型、持续高写入长期压力、首次配对、Watch、热点切换、Linux。残留：全局输入版本的保守重算、批量短写争用、端到端确认前后对照缺失。

## 交付方式

正常推送两端origin/master，不force push；最终HEAD/ls-remote和固定版本链接由交付回复给出。审核包从最终已提交HEAD原始字节导出，基线为本文起始提交，包含两端源码/直接依赖/测试/合成夹具/本文/diff/逐文件SHA，复核内部契约。仅留既有忽略的outputs目录，不含数据库、音频、真实转写、密钥、HAP、原始报告或.git，不上传第三方。
