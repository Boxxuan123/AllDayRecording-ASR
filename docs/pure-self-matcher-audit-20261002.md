# Pure self matcher — 固定资产诊断（2026-10-02）

## 1. 结论

人工逐片段确认纯净的 4 个 7.52–8 秒 Self 事件，当前纯 matcher 全部识别为 Self（4/4，FN=0）；49 个完整 Non-self 事件没有 False Self（FP=0）。但 Self 全部有既往人物学习暴露，不能宣称独立总体识别率。短音频 crop 发生 2 次误收，来自同一个 Non-self 事件。

## 2. 冻结版本与资产

- ASR 测试代码：`9f69efdcb678563cd579793af66323dfb0853138`；Phone：`06435e3c1dd9963480a15f4c7b1860b142644a9c`（NO CHANGE）。
- 20 条 enrollment 原音均存在且 SHA256 与 NPZ metadata 一致；82×192 reference 和 192 维 centroid 可加载，维度及非零范数正常。
- CAM++，FunASR 1.4.4，当前 single-waveform-v2 协议。现有 active policy accepted、无 blockers；未改变任何生产实现。
- Self threshold=`0.13760416209697726`，Non-self threshold=`-0.006270442157983781`；policy version=`v3.1-self-2f513e0b062d`。
- Voiceprint SHA256=`dbee01f6cd6b0b5ea222aa8aba1b2aae59da7282a3a647d69b7d2176ab105abe`。Calibration、policy 和模型权重摘要保存在私有指纹文件。真实 person ID 与资产路径只放私有 REPORT。

## 3. Ground truth 与泄漏隔离

只接受现存逐片段人审 `clean_single`、明确 primary person、无 conflicting review 的原始范围。535 条 human Self annotation 不自动晋升为干净真值：现有标注入口允许批量选择，legacy/cluster/自动预测也不用于正式 truth。没有调用 ownership/diarization 来挑选纯 matcher 的音频。

169 个已复核 source 范围：61 个非明确 clean-single，5 个短于 1 秒，41 个与 enrollment/calibration 范围重叠被排除；余 62 个边界按近邻、共享 utterance 和音频重叠合并为 53 个事件。Self 4 个、Non-self 49 个。目标 50–100 个 Self 未达到，没有用自动标签凑数。

初始逐范围泄漏检查留下 12 个 Non-self；进一步按源文件隔离，将其中 5 个与 learning/fitting 共享文件的事件移入 secondary。这个隔离只使用来源摘要，不读分数，不重新选事件，不重新评分；冻结 manifest、truth 和原始 scores 保留，最终分组以 cohort-provenance-review.json 为准。

| Group | Truth | Events | Sessions | Dates | Duration ms |
|---|---|---:|---:|---:|---:|
| primary | self | 0 | 0 | 0 | 0 |
| primary | non-self | 7 | 4 | 4 | 27680 |
| secondary_exposed | self | 4 | 1 | 1 | 31440 |
| secondary_exposed | non-self | 42 | 13 | 10 | 249050 |

Primary 没有 Self，不能计算 recall/FN rate；7 个 Non-self 也只是小规模 diagnostic。Secondary 的 4 个 Self 均同一天、同一 session，曾进入人物学习样本，但未用于 standalone self enrollment/calibration；pure matcher 不读取这些人物样本。42 个 secondary Non-self 同样明确标记学习/拟合接触。既有 purity audit 包含 failure/self-like/profile 目标，属于有偏回顾性选择，不是随机总体样本。

## 4. Primary pure matcher 的完整事件结果

| Duration | Unexposed Self N/accepted | Unexposed Non-self N/False Self | Secondary Self N/accepted | Secondary Non-self N/False Self |
|---|---:|---:|---:|---:|
| 1-2s | 0/0 | 2/0 | 0/0 | 6/0 |
| 2-3s | 0/0 | 0/0 | 0/0 | 3/0 |
| 3-4s | 0/0 | 3/0 | 0/0 | 0/0 |
| 4-6s | 0/0 | 0/0 | 0/0 | 8/0 |
| 6s+ | 0/0 | 2/0 | 4/4 | 25/0 |

固定 matcher 仍保留 duration/12000 的质量下限；<2 秒即使 raw score 越过 Self threshold，也返回 Unknown。TN 表示 not Self，包含 Unknown 和 not_self，不表示已成功辨认别人。完整事件：primary TP/FN/FP/TN=0/0/0/7，secondary=4/0/0/42。

## 5. 时长敏感性（相关 crop，不能增加 event N）

每个允许的人工干净 event 取居中 1.5、2、3、4、6 秒 crop；不添加 VAD、降噪、额外窗口 gate。下面合并两种暴露分组，只用于诊断。

| Crop | Self N | Accepted | Non-self N | False Self | Self min / median / max | Non-self min / median / max |
|---|---:|---:|---:|---:|---|---|
| 1500 ms | 4 | 0 | 48 | 0 | 0.061645 / 0.310311 / 0.471746 | -0.183245 / 0.019858 / 0.189796 |
| 2000 ms | 4 | 4 | 39 | 1 | 0.161899 / 0.346724 / 0.424064 | -0.224784 / 0.014538 / 0.139573 |
| 3000 ms | 4 | 4 | 38 | 1 | 0.234735 / 0.389735 / 0.470897 | -0.224037 / 0.002529 / 0.161588 |
| 4000 ms | 4 | 4 | 35 | 0 | 0.346128 / 0.408364 / 0.485945 | -0.206711 / -0.006118 / 0.116202 |
| 6000 ms | 4 | 4 | 27 | 0 | 0.387037 / 0.438981 / 0.524268 | -0.189180 / -0.011827 / 0.113195 |

1.5 秒 Self 为 0/4，全部被既有最低质量规则拒绝；其中 3 条 raw score 仍高于阈值。2、3、4、6 秒 Self crop 都为 4/4。Non-self 的同一事件在 2 秒 score=0.139573、3 秒=0.161588，均 False Self；它的完整 6 秒以上 event 不误收。不能以 crop 的 2 次误收冒充 2 个独立 false-positive event。

## 6. Score distribution

| Full group | Truth | Min | P25 | Median | P75 | Max |
|---|---|---:|---:|---:|---:|---:|
| primary | Self | — | — | — | — | — |
| primary | Non-self | -0.091197 | -0.057185 | 0.011735 | 0.062672 | 0.068000 |
| secondary_exposed | Self | 0.410817 | 0.423188 | 0.433319 | 0.464446 | 0.539811 |
| secondary_exposed | Non-self | -0.172972 | -0.033721 | 0.000386 | 0.025072 | 0.180501 |

完整事件 pooled Self min/median/max=0.410817/0.433319/0.539811；Non-self=-0.172972/0.001278/0.180501。本批完整 score 区间不重叠，间隙约 0.230316；但短 crop 分布有重叠和误收。不能把少量、有偏且已接触样本的分离推广到所有日常声音。每桶双方 min/p25/median/p75/max 及 raw samples 在 JSON/CSV 中。没有扫阈值、调阈值或拟合。

## 7. 长音频

| Scope | Self N / accepted / rejected | Non-self N / False Self |
|---|---:|---:|
| 4s+（含 secondary） | 4 / 4 / 0 | 35 / 0 |
| 6s+（含 secondary） | 4 / 4 / 0 | 27 / 0 |

4 个 6s+ Self score：0.410817、0.439325、0.539811、0.427312。没有发现这批干净长 Self 大量低于阈值的证据；尚无未接触 Self 或跨日 Self，不能排除其它场景的 enrollment/calibration 失配。

## 8. Production-vs-Pure 与 Failure Taxonomy

先完成 pure 评分，再在内存 SQLite backup 中恢复原 automatic speaker track、移除 person_annotation，仅诊断既有 query builder。正式库不写入；真实生产中手工身份由 `preserved_manual_person` 保留，不应把这个优先级保护叫模型失败。

配对 Self 4 个：pure 4/4；automatic counterfactual 2/4。2 个成功事件的人审范围和 utterance 范围完全一致；另外 2 个原 utterance 长于审核音频，明确记录边界差异。补充把内存 query 输入限定为相同人审边界后，结果仍为 2/4：1 个 QUERY_OWNERSHIP、1 个 QUERY_FOREIGN_SPEAKER_OR_OVERLAP。自动 evidence 与已确认纯净人审边界不一致；没有修改 evidence 或 query builder 来让它通过。

完整 Self event：MATCHER_FALSE_REJECT=0、PASS=2、QUERY_OWNERSHIP=1、QUERY_FOREIGN_SPEAKER_OR_OVERLAP=1。Primary 和 paired 分开，query 拒绝不能归入 matcher false reject。

## 9. Named-person independence

**NO — pure self matcher is independent of named-person profiles.**

`tools/audit_pure_self_matcher.py:pure_infer` 只调用现有 FunASRBackend 和 CalibratedSelfIdentityMatcher；预处理直接复用 `adapters/audio/tools.py:extract_clip` 的 16k mono PCM。`adapters/self_identity.py:_load` 只读 active policy 和绑定 NPZ；`match` 只计算 0.7×self centroid + 0.3×top-3 self references，不读取 named-person profile、不参与其它人物竞争。Profile/annotation 被读取仅用于泄漏排除和完整性摘要，不参与 pure score。

## 10. Historical Backfill 0 Auto 的解释

上轮 2,013 Unknown 中，760 foreign/overlap、603 时长不足、220 缺少证据、157 ownership、88 excluded sound、3 质量不足、178 low score，另 4 个单窗进入 review；未知的既往路径不能猜成“旧代码未运行”。本轮表明这 4 段已确认长 Self 的 voiceprint/matcher 能匹配，也实际复现 pure 成功而 query 被挡。可以确认 gate/query 至少是部分瓶颈，但这 4 个事件不是上轮整个 Unknown cohort 的独立真值，不能据此断言全部 0 Auto 都与 matcher 无关。

## 11. Batch invariance 与 sanity

- CPU：随机固定 seed 抽 20 个 evaluation full event，单条/重复/短伴随/长伴随/反序；max embedding drift=0.000000000，max score drift=0.000000000，decision flips=0。
- CUDA：随机固定 seed 抽 20 个 evaluation full event，单条/重复/短伴随/长伴随/反序；max embedding drift=0.000000000，max score drift=0.000000000，decision flips=0。
- CPU↔CUDA max score difference=0.000445215，decision flips=0。这不是同设备 companion 漂移；跨设备数值差异保留报告。ENGINEERING VALID=True。
- 请求 batch 参数仍测试，底层模型按已修复协议始终单 waveform 推理；没有绕开适配器重演已弃用 padding 协议。
- Enrollment 原音前 8 秒 probe：20/20 Self，**SANITY ONLY**；不能算 evaluation recall，也不能冒充重新生成 82 条原 reference（原 chunk 时间戳不在 NPZ）。

## 12. Integrity 与测试

此次 before/after 全部数据库表指纹完全一致，不仅是研究表；生产 identity、correction、profile/prototype、annotation truth、learning exposure、Blind/holdout/independent、enrollment、reference、calibration、policy 和模型权重均 unchanged。原音仅被读取，裁剪输出仅位于 Git ignored outputs。

基线完整 tests：547 passed、1 skipped、2 既有架构失败。新增审计测试 18 passed，完整最终 tests：565 passed、1 skipped、相同 2 架构失败（维护行数限制与 application/domain/ports adapter import，违规清单不变）。Ruff、compileall、py_compile、git diff --check 通过。未修改生产或手机代码。

两项既有失败（`tests/test_v3_architecture.py::V3ArchitectureTests`）：

- `test_computer_production_sources_stay_below_maintenance_threshold`
- `test_domain_application_and_ports_do_not_import_ui_http_models_or_sqlite`

18 项审计测试和原 self regression 合并运行：28 passed。失败明细逐行对照基线一致，未为通过架构限制修改无关生产代码。

## 13. 决策表与下一步

| 观察 | 可以得出的结论 | 下一步 |
|---|---|---|
| 4 个干净 6s+ Self 全通过，完整 Non-self 无误收 | 当前资产至少能匹配这些长音频；独立 Self 证据不足 | 增补跨日、未接触的人工干净长 Self/Non-self，再复核 |
| Pure 成功而同边界 query 2 个失败 | ownership/foreign evidence 是这两例的阻断点 | 优先单独诊断 query construction / diarization 边界；不立即改生产 |
| 同一 Non-self event 的 2s/3s crop 误收 | 短音频分离存在风险 | 保持保守 gate；本轮不降阈值 |
| 没有发现长 Self 大量拒绝或长 score 严重重叠 | 暂无依据立即重 enrollment/calibration 或换模型 | 不从本批有偏样本挑选新阈值 |

责任分类：C/D 有直接配对证据；E 有短 crop 误收/质量拒绝；F 是总体结论的主要限制。A/B 尚未被证明，也没有被小样本排除。下一轮优先 query construction 的定位，并补独立真值；暂不选择重注册声纹或 benchmark 新模型。

## 14. 复现与 Git

新增工具默认只读，显式输出目录，拒绝覆盖已有 frozen manifest / scores：

```powershell
python tools/build_pure_self_eval_manifest.py --state-dir state/v3 --legacy-db state/allday_asr.sqlite3 --prior-manifest outputs/short-self-matcher/split-manifest.json --output outputs/pure-self-matcher-audit-NEW
python tools/audit_pure_self_matcher.py --state-dir state/v3 --output outputs/pure-self-matcher-audit-NEW
```

私有详细结果目录：`outputs/pure-self-matcher-audit-20261002/`。包括 frozen manifest/summary、scores JSON/CSV、final provenance review、score-distribution JSON/CSV、duration buckets、failure taxonomy、production paired/exact、batch invariance、前后指纹和 REPORT。真实音频、姓名、完整转写、原始 utterance ID、私人路径均不提交。仅提交审计工具、测试和本匿名报告；生产代码保持测试基线。ASR 正常推送 origin/master 并核对三方 SHA；最终 40 位 commit 随交付给出。Phone NO CHANGE。
