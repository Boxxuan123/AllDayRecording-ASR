# Speaker Turn Preservation + Boundary-aware Blind Query

## 根因与边界

旧投影按 token speaker、1200 ms gap、30 s hull 合并，未检查 gap 中已有的其他
exclusive turn。短回应缺少可用 ASR token 时，同 label 文本可能重新包住外来原音。
旧 Blind builder 则每个 utterance 只取首个 capture 的前 8 s，静默舍弃后续来源。
最新人工 coarse mixed verdict 的 first_bad_layer 仍为 UNKNOWN；不能据此认定
diarization 是主因。

speaker track 是 run 内身份分组，不能证明音频在时间上连续。exclusive speaker turn
是投影和连续 query window 的一等边界。regular overlap prediction 单独保留，不被
当成这项 exclusion 规则。

## P0

domain/speaker_turns.py 定义 merge invariant：同 attribution、gap ≤1200 ms、
总 hull ≤30 s，且 hull 内无 foreign exclusive turn。native_projection 的默认实现
采用这一规则，生产 pipeline 传入 exclusive turns。

没有 B token 时只分开 A-before/A-after，不生成空 B 文本。token 本身跨其他
exclusive speaker 时，文字、时间戳与 source refs 保持一次完整保存；多数 attribution
保留于 token_attributions，transcript 则将该 token 单独列为 boundary-uncertain、
未分配身份的 utterance。它不被当成纯净 A 来源继续合并。未来 enrollment 自然复用
共享上游投影；现有人工原音证据不变，新 range 不继承旧 purity verdict。

标记：exclusive-turn-preserving-v2。旧函数 _group_utterances_legacy 可用于对照重放，
旧审计工具明确绑定旧函数，保持审计结果的历史语义。

## P1

domain/blind_windows.py 从 utterance、exclusive turn、ASR token、VAD 和 capture
mapping 规划单媒体连续窗。先扣除 foreign exclusive intervals，再在上限前 1200 ms
搜索合理边界，依次采用同 speaker turn end、token end、VAD end。不会选在 token
中央的候选边界；没有候选时采用显式 hard_cap。

长度参数未变：每窗 800–8000 ms、每 query 至多 5 窗/40 s。长来源规划后再选择预算
内 windows；超预算、重叠来源、短尾、缺失或无效 mapping 都写明原因。
只在同 session、严格 timeline adjacency、有效 source mapping 时跨 capture 继续，
分别提取 window embedding，再使用原有 unit mean aggregation。文件边界不再触发
无信息的 break。

每窗记录 media/source/session range、原 utterance、track/label、exclusive coverage、
projection/query version、start/end boundary reasons 和 capture continuation。
模型输入和审核输入使用同一组范围；review_window_order 按原始时间排序，
model_window_order 保存模型顺序。手机标明 multiple clips，不追加 Blind context。
标记：turn-safe-boundary-aware-v2；旧 longest5-head8s-v1 可显式重放。

## Blind Review / Experiment V2

审核 schema 2 分为 speaker_composition（clean_single、simultaneous_overlap、
sequential_multi_speaker、backchannel、uncertain）与 boundary_quality（clean、
cut、uncertain）。手机默认边界正常；不显示模型答案或分数。SQLite migration 24
保留所有 V1 coarse verdict 为 schema 1，不猜测细分类别；append-only 提交、修改、
撤销与 operation id 重试均保留。旧任务仍可审核，旧实验报告按所属版本更新。

upgrade_turn_experiment 原样复制 active V1 的 frozen model、model files、Legacy、
Clean Profile V1、G/P/margin、matcher 和 shadow probe 参数；原子退休 V1 并冻结
V2 的 projection/query/review 版本、当前数据截止点及全部已有音频指纹。
旧 session 与已见媒体不计为新的 prospective V2。报告仅计算所属实验分母，
保留 V1 historical diagnostics；不会将 V1/V2 汇总成总体 accuracy。
Blind reservation/enrollment exclusion 保持原逻辑。

## 运行与回退

只读 replay：

```powershell
.venv/Scripts/python.exe tools/replay_speaker_turn_preservation.py --database state/v3/core.sqlite3 --audit outputs/speaker-turn-boundary-audit-20261001 --output outputs/speaker-turn-preservation-20261001
```

备份数据库、部署代码后，使用 freeze_blind_turn_v2.py 的 --database、--model-root、
--output、--experiment 参数冻结本地 V2；该命令不启动 worker，不重建 profiles，
重复运行返回原冻结版本。随后重载正常电脑服务。模型文件必须匹配原冻结指纹。

回退先关闭 collection 并退休 active V2。旧投影与 builder 仅用于明确版本的 replay；
如回退生产上游实现，仍保留 schema 24 与 V1/V2 审核兼容代码。不要恢复旧数据库来
覆盖新增人类证据，也不要修改 frozen V2 或将其样本并回 V1。恢复采集需要另建明确
语义的新实验版本。

## 验证

私有只读回归：18 个旧 downstream bridge →0；逐 token 来源与文字保存检查通过。
旧 13 个裁短案例：13 个首窗采用自然边界，12 个拆为多个安全窗，6 个继续到其他
capture，0 个仍需 hard_cap；上述分类可重叠，6 个包含旧 capture-cut 的 5 个案例。
回放全部新 query 与这些窗口均没有跨明确 foreign exclusive turn。
这些是模型预测结构候选，不是人工 backchannel truth，也不证明声纹准确率提高。

45 项相关单元/集成回归覆盖 projection、planner、V2 freeze/isolation、审核历史与
source identity。完整测试另有两项既有架构失败，涉及未改动源文件长度及依赖方向；
其余 409 项通过、1 项跳过。ruff 与 Python 编译通过。
手机 Hypium 157/157、V1 SQLite 回归、六类 V2 SQLite 离线/重启/提交/修改/撤销/
同步回归及 assembleApp 构建通过。实机安装状态与运行时就绪状态保存在私有
verification.json；未确认安装新版手机客户端前，不宣告端到端采集就绪。

所有回放、原音来源、人类证据、frozen vectors、正式数据库备份与验证细节都留在
ignored outputs/speaker-turn-preservation-20261001/。公开文件没有私人原音或标识。
Diarization/CAM++/VAD 模型、G/P/margin、生产 profiles、Clean Profile V1、自动确认
与 enrollment 采样方案未改变；Query Purity Probe 仍 shadow-only，不启用 abstention。
