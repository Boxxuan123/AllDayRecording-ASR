# Automatic blind shadow validation

第一次安装在本机冻结实验一次：

```powershell
python tools/blind_validation.py initialize
python tools/blind_session.py enable-collection
```

默认初始化路径指向既有私有 clean V1、model lock 和 frozen matcher 文件；
也支持 --clean-profile、--model-lock、--matcher、--clean-version、--experiment。
V1 绑定两组 profile vectors/source provenance、相同人物库、CAM++ 文件 hash、
原 G/P/minimum speech/margin、matcher version 与历史原音 hash 清单。当前
自画像不作为普通 target，自声音通过人工库中 self kind 自动进入 hard negative。
初始化不写 production prototype、profile 或 threshold。重新初始化不同内容
必须换版本；新实验前显式 retire 原版本，不能覆盖旧快照。

正常电脑服务启动时启动 durable worker。处理完成后 people.analyze 对 blind
只排 shadow job，holdout 保持冻结；ASR、diarization 和 speaker track 正常运行。
worker 也周期发现已处理的 blind session，覆盖重启/漏通知恢复；失败记入 SQLite，
带退避重试，运行 lease 过期可重新 claim。失败不使上传失败。

自动 query 只读取原始 processing run、original speaker track 和原音范围，不
读取人工人物/声音修正或 blind truth。最长五段、单窗最多八秒，全部保留用于
播放；同一组 CAM++ window embedding 平均得到两个 arm 共用 query。
身份 scoring 使用最大 profile cosine、冻结的 G/P 与 margin。query probe 只
记录 shadow 状态，不调参，不改变身份决策或过滤主指标。可在初始化时传入
既有 --probe-thresholds；未冻结阈值时记 INSUFFICIENT，不临时训练。

预测先冻结，然后产生 blind_identity_review。如果普通人物事实先于预测、
原音在冻结前已使用或录音早于实验 cutoff，该 job 显式 blocked，不能伪装成
独立 blind。冻结前原音 hash 不限于已 enrollment 音频，包含历史学习/诊断。
一次追加处理只新增未出现的范围 query；重复处理不重复 role/prediction/task。

事件按原音 SHA + 范围重叠，或同 session/track 五秒内邻近、同 cluster 两秒
内邻近做传递闭包；不同 session 的重传原音同样合并。保留全部 query views 和
合并边。每个事件只审核一个代表 query：首次按时长/稳定 key 选取，以后合并
沿用最早事件代表，不依据真值/成绩重选。其他 views 只保留诊断预测，不自动
继承代表的人物真值。被合并事件 superseded，不重复计数。

手机「审核箱 → Blind 人物验收」复用纯度页。提交前只显示完整音频、人物库
与纯度选项；没有预测、score、算法意见。库中未 enrollment 的人物仍按所选
person ID 保留；陌生/未建档、不知道、听不清分开保存。提交后移入历史，支持
离线持久化、下一条、skip、undo、revise。不要提前在普通标注页给未冻结的
blind query 做人物标签。Prediction 与 append-only ground truth 分表，数据库
要求 prediction_created_at < reviewed_at。Undo/修订只追加 truth/evaluation。

每次 job 和 truth 提交后自动更新本机私有目录：

```text
outputs/speaker-blind-shadow-validation/
  experiment.json
  session-roles.json
  blind-events.json
  prediction-snapshots.json
  review-progress.json
  event-level-results.json
  query-view-diagnostics.json
  current-report.json / current-report.md
  verification.json
```

每个实验也有独立子目录。report export 失败不丢真值，worker 重试导出。
`python tools/blind_validation.py report` 可随时刷新；`retry --session <local-id>`
可立即重试 failed job。blocked 不是可洗掉的 transient error。

主指标单位是独立事件的固定代表 query，报告 Legacy/Clean × G/P：known
correct/reject/wrong、unknown reject/FA、self→other。给出分母与 Wilson 95%
区间、两组差值、purity strata、session/date/person 覆盖、self hard negatives。
人工 mixed/boundary/uncertain purity 不过滤主指标；不知道/听不清是身份真值
未解决，单独计入 unresolved coverage，不能冒充陌生人的正确拒绝。

保守证据门槛在初始化快照内预先登记：至少 30 事件、5 sessions、3 日期；每个
known 至少 3 事件/2 sessions；unknown 至少 10 事件、self 至少 5 事件。
达不到时显示 INSUFFICIENT BLIND EVIDENCE。区间是事件层描述，session
集中性仍需看覆盖；不自动判定赢家，不建议自动上线，也不回流做方法选择。

原音、人物映射、embedding、review、预测、指标和本机备份只能在 ignored
outputs/state 路径保存。公开仓库只包含协议、实现和合成测试。
