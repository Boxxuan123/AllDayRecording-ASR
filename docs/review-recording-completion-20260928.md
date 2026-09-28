# 录音完成协议修复评审（2026-09-28）

## 结论

暂不提交或推送：发现 3 项功能阻塞和 1 项契约版本错误。

这条既有录音的数据恢复已经成功。只读核对正式 Core：会话 `69WRQ94VSEQVX0TPXN0ASQSYHC` 有 71 个分片，覆盖 4,227,580 ms；输入版本 2 的运行 succeeded，983 条当前转写，原 7 条保留为 stale。本次评审未修改正式数据库、音频或设备数据。

## 1. [P1] 结束记录没有贯通持久队列及 Wi-Fi 传输

新增的 `WatchRecordingViewModel.ets:134` 将 `session_summary.json` 交给 `enqueueAutomatic`，但公共传输层仍只认音频：

- `common/src/main/ets/sync/watch/AutomaticSyncQueue.ets:132` 的清单校验仅允许 WAV/M4A。`enqueue()` 会把 JSON 加入内存，但随后 `writeVersionedJson()` 校验失败，返回 `enqueued_not_saved`。只要 JSON 仍在队列中，后续任务也无法持久保存。
- `common/src/main/ets/services/WifiRecordingTransferService.ets:190` 的来源路径校验拒绝 JSON；同文件约第 701 行的扫描也会过滤掉 JSON。修改 WearEngine 手动扫描不能解决 Wi-Fi 路径的问题。

实际执行生产队列代码和 JSON 校验代码，使用注入的合成文件系统，结果：

```json
{
  "wav_enqueue": "enqueued",
  "summary_enqueue": "enqueued_not_saved",
  "next_wav_enqueue": "enqueued_not_saved",
  "in_memory_count": 3,
  "restore": "restored",
  "restored_count": 1,
  "wifi_accepts_summary": false
}
```

影响：断线期间停录再重启，会失去尚未持久保存的结束记录及后续队列项；音频文件仍在磁盘，但自动恢复不完整。Wi-Fi 同步不能传递新协议要求的结束记录，手机会一直等待结束证明。

修复：统一支持受限目录内的结束记录类型，更新队列写入/恢复校验、Wi-Fi 扫描和接收路径校验。增加“WAV → 结束记录 → 后续任务 → 重启”和 Wi-Fi 发送/接收的集成测试，验证结束记录最终到达手机并触发完整清单提交。

## 2. [P1] 新版本清单仍写入旧版本备份目录

`src/allday_asr/v3/adapters/backup/filesystem.py:98` 已读取最新版清单，但 `backup()` 第 54 行仍固定使用 `backup_root / session_id`。已有 v1 备份时，v2 新清单的哈希必然不同；第 73 行按新清单核验旧目录，会报告损坏。

隔离复现使用真实 UploadStore、V3 入库及备份适配器：

1. 上传并入库单片的 v2 协议清单，创建第一版备份。
2. 上传第二片，以合法追加方式得到输入版本 2。
3. 对同一 backup_root 再调用备份。

实际结果：`RuntimeError: backup restore drill found corrupt content`。正常自动流程在进入新版转写前就会失败。已有恢复记录使用单独的 `session-backups-repaired` 根目录，只绕过了这条录音的冲突。

修复：让备份目的地绑定输入版本或清单摘要，保持旧备份不可变；备份结果携带本次实际核验的输入版本和清单摘要，准入证据直接使用这个结果。增加同一根目录下 v1→v2→重试 v2 的回归测试。

## 3. [P1] 旧任务运行期间提交新输入，没有安排后续处理

`src/allday_asr/v3/adapters/transfer/automation.py:102` 只修正了 completed 状态的版本检查。第 105 行仍按 session_id 判断 `_active`：旧版本运行时，新版提交不会写入待处理版本，也不会排队。运行结束的 finally 仅移除 `_active`，未检查是否还有更高输入版本。

调用真实 `submit()` 方法，设置旧运行 input_revision=1、当前数据库输入版本为 2 且该会话 active，观测结果：

```json
{
  "returned_revision": 1,
  "queued_count": 0,
  "persisted_changes": 0
}
```

影响：旧版本顺利结束时，新清单虽已入库，当前进程仍不会自动补跑最新输入；手机保存完整清单回执后也可能不再提交它。正常轮询只处理重试状态，不能保证补回这次丢失的请求。

修复：持久记录会话的目标输入版本；每轮结束检查是否落后并排队。处理各阶段读取和发布结果时也要核对版本，避免旧任务使用追加后的实时输入或覆盖新版状态。增加旧任务运行中追加、追加后重启、并发重复提交的回归测试。

## 4. [P2] 发布契约声明落后于实际迁移

`contracts/v3/release-lock.json:5` 仍声明 `core_schema_version=17`、`phone_projection_schema_version=12`；本次代码实际已增加电脑迁移 18 和手机迁移 14。

当前测试 `V3ContractTests::test_manifest_is_the_only_versioned_contract_index` 实际失败，断言为 `17 != 18`。修复两个版本字段后，还需要用项目契约同步流程更新 manifest 摘要及另一仓的 source 锁，不能只修改断言。

## 验证范围与结果

| 验证 | 结果 |
| --- | --- |
| 电脑上传、设备同步、持久处理、同步开销/传输、契约、说话人相关 pytest | 92 通过，1 失败；失败项是上述契约版本错误 |
| 手表 Hypium 宿主测试 | 67/67 通过 |
| 手机 Hypium 宿主测试 | 84/84 通过，无失败、错误或忽略 |
| `check-manifest-recovery.cjs` | 通过，验证现有逐片/清单/失败回执保存与重开 |
| `check-upload-offset.cjs` | 通过，验证上传断点、哈希及已确认文件跳过/补传 |
| 关键修改文件 Ruff、两仓 diff whitespace 检查 | 通过 |
| 本次新增的隔离边界复现 | 复现结束记录持久化失败、Wi-Fi 路径拒绝、备份目录冲突及在途新版提交未排队 |

测试执行说明：默认宿主测试脚本的 phone product 在当前本地工程配置下没有匹配 target，脚本正确拒绝了旧字节码。随后按当前配置用 `product=default`、`module=phone@default` 重新编译执行，得到本次新生成的 84/84 报告。没有复用旧报告。

证据保存在两仓各自的 outputs：

- 电脑：`review-recording-repair-20260928.log/.xml`、`review-append-backup-20260928/result.json`。
- 手机工程：`review-completion-transport-20260928.json`、`review-recording-host-20260928.log`、`review-phone-host-20260928.log`。

现有测试覆盖了修复中的多个单独组件，但没有覆盖上述端到端连接处。本轮保持所有业务源码原样，仅增加评审文档和本地诊断输出；未执行 git add、commit 或 push。

## 修复跟进（2026-09-28）

上述四项均已在工作区修复：

1. 自动队列对受限会话目录中的 `session_summary.json` 做写入和恢复校验；Wi-Fi 扫描、发送与接收接受同一受限路径，并限制结束记录大小。新增音频、结束记录、后续音频及重启恢复测试。
2. 独立备份目录按会话、输入版本和清单摘要隔离；备份结果携带实际核验的版本及摘要，准入证据直接引用该结果。新增同一根目录下 v1、v2 和 v2 重试测试。
3. 在途追加会持久记录目标输入版本；旧任务结束及服务重启后补排新版。处理阶段检查固定输入版本，避免旧任务以新版实时输入提交或标记完成。新增在途重复提交、旧任务结束和重启恢复测试。
4. 发布锁更新为 Core 18、Phone 14，并通过契约同步脚本更新清单摘要和手机仓 source 回执。

验证：电脑相关 pytest 95/95；手表宿主测试 68/68；手机宿主测试 84/84；两端安装包编译通过；契约摘要检查与 Ruff 通过。手机新版已安装并保留数据。手表当前未连接，Wi-Fi 实机传输尚未执行。未修改正式录音、数据库或备份数据，也未提交或推送。

## 再次复核（2026-09-28）

本轮重新执行：电脑 pytest 95/95、手表 68/68、手机 84/84，以及清单回执恢复、上传断点检查均通过；关键文件 Ruff 和两仓 diff 检查通过。前三类中的结束记录传输、分版本备份，以及契约版本问题已解决。

仍有一项阻断：在途追加只终止了自动流程的等待，没有终止数据库中的旧版 processing job。`automation.py` 捕获 `SupersededInput` 后直接继续排新版；新版循环调用的 `DurableProcessingWorker.run_once()` 从全局队列领取任务，不限定当前会话或输入版本。`claim_next()` 仍能领取旧版任务，而原生 ASR 和说话人阶段通过会话读取当前全部分片。外层检查的是新版目标，因此无法识别内部实际执行的旧版任务。旧版可能继续消耗完整转写时间，并把不同输入阶段的产物以同一旧版 run 发布。

隔离测试复现：提交输入版本 1，完成首阶段，再插入版本 2 的修订记录；再次调用真实 `DurableProcessingService.claim()`，得到 `current_input_revision=2`、`claimed_input_revision=1`、`claimed_stage=backup_admitted`、`old_job_status=running`。输出在 `outputs/review-stale-job-20260928.json`。未操作正式数据库。

修复建议：在持久任务层取消或淘汰旧输入版本任务；领取、心跳及阶段提交时核对实际 claim 的输入版本，阶段结果提交检查需要和版本读取处于同一事务，避免检查后追加的竞态。若允许旧任务继续，则须使用按输入版本固定的分片快照。补真实数据库和真实 Worker 的在途追加测试，断言旧任务不能再领取或提交投影，新版本仍能完整完成。现有新增测试替换了 `_process`，未覆盖这一层。

本轮仅追加评审记录和隔离诊断输出，未修改业务代码，未提交或推送。

## 持久任务层修复跟进（2026-09-28）

再次复核提出的阻断已修复。持久处理服务在领取任务前淘汰版本过期的排队任务；运行中旧任务的心跳会请求取消；阶段完成和失败提交在持有 SQLite 写事务时核对 claim 的输入版本，过期结果不写入音频产物、转写或手机投影。旧版本任务也不能重新进入手动重试队列。自动流程仍负责排入最新输入版本。

新增真实 SQLite 与真实 `DurableProcessingWorker` 测试，覆盖旧任务完成首阶段后追加、执行中追加、心跳取消，以及过期阶段携带转写和工件时无法提交。相关电脑测试 99/99 通过，Ruff 与 diff 空白检查通过。本次未修改正式录音和数据库，未提交或推送。

## 最终复核（2026-09-28）

已核对持久任务层的领取过滤、旧任务取消、心跳取消及同一写事务内的结果版本校验。重新运行相关电脑测试，99/99 通过，关键修改 Ruff 和 diff 检查通过；上轮手表 68/68、手机 84/84 的测试所覆盖代码本轮未变化。本次复核未发现仍需阻断提交的问题。Wi-Fi 真机传输仍未在本次复核中验证。提交包含同步修复、相关优化、契约、测试与文档；三个绑定特定录音的一次性修复脚本保留本地。
