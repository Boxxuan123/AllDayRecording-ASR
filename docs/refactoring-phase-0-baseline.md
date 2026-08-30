# 可维护性重构阶段 0 基线

> 状态：完成  
> 基线日期：2026-08-30  
> 适用提交：开始移动生产代码前的当前工作区

本文件把重构期间不得无意改变的 CLI、HTTP 和 workflow 行为固定下来。
它描述的是当前契约，不代表这些接口已经完成分层。

## 1. 可重复验证命令

在仓库根目录使用项目虚拟环境：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
ruff check src tests
```

只运行阶段 0 新增的快速基线：

```powershell
.\.venv\Scripts\python.exe -m unittest -v tests.test_cli_smoke tests.test_architecture
```

阶段 0 开始前的完整回归结果：

- Python：项目 `.venv` 中的 Python 3.12。
- 命令：`.\.venv\Scripts\python.exe -m unittest discover -v`。
- 结果：79 个测试通过，耗时 21.073 秒。
- 测试使用临时 SQLite 和合成 WAV；本轮未加载真实模型、未读取真实录音。

阶段 0 新增 4 个 CLI smoke test 和 3 个架构基线 test。新增后再次运行完整
套件：86 个测试通过，耗时 28.472 秒。耗时只用于诊断，不作为失败条件。

Ruff 版本固定为 `0.13.1`，规则在 `pyproject.toml` 中显式选择：

- `E4`、`E7`、`E9`：基础语法、缩进和运行前错误；
- `F`：Pyflakes；
- `B`：flake8-bugbear；
- `cli.py` 按 Typer 惯例忽略 `B008`；
- 三处历史 `zip()` 仅按文件忽略 `B905`，避免把历史清理混入本阶段。

## 2. CLI smoke 契约

| 场景 | 调用 | 预期退出码 | 隔离要求 |
| --- | --- | ---: | --- |
| 根帮助 | `allday-asr --help` | 0 | 不加载模型；输出包含根用法和 `workflow-v2` |
| 参数错误 | `allday-asr process 0` | 2 | 在 Typer 参数校验阶段结束；不打开数据库或音频 |
| 成功退出 | `allday-asr recordings --db <empty-db>` | 0 | 只初始化临时 SQLite；不读取真实音频 |
| 失败退出 | `allday-asr doctor`，注入一个失败检查 | 1 | 不运行真实环境、CUDA 或模型检查 |

这些契约由 `tests/test_cli_smoke.py` 执行。后续拆分 `cli.py` 时，命令名、
退出码和上述最小输出含义必须继续成立。

## 3. Web 公共约束

主工作台和盲标工作台都只允许回环地址。除带 `token` 的首次根路径外，所有
读取请求都要求 `X-AllDay-Token` 或安全 Cookie；写请求还拒绝非本地同源的
`Origin`。

| 情况 | 当前结果 |
| --- | --- |
| 首次 `GET /?token=<valid>` | `303`，设置 `HttpOnly; SameSite=Strict` Cookie |
| token 无效 | `403`，JSON 顶层字段 `error` |
| 未认证读取或写入 | `403`，JSON 顶层字段 `error` |
| 非同源写入 | `403`，JSON 顶层字段 `error` |
| 参数、JSON 或业务值错误 | `400`，JSON 顶层字段 `error` |
| 文件或键不存在 | `404`，JSON 顶层字段 `error` |
| 未匹配路径 | `404`，JSON 顶层字段 `error` |
| 未处理异常 | `500`，JSON 顶层字段 `error` |

成功 JSON 默认使用 `application/json; charset=utf-8` 和 `Cache-Control:
no-store`。音频响应使用 `audio/wav` 和私有缓存。所有响应继续发送现有的
`nosniff`、`DENY`、`no-referrer` 和 CSP 安全头。

## 4. 主工作台端点清单

下表中的“顶层字段”是兼容性快照；带星号的行会按“尚无结果/已有结果”返回
所列字段的相应子集。

### 4.1 读取与静态资源

| 方法 | 路径与必要参数 | 成功状态 | 响应类型 / 顶层字段 |
| --- | --- | ---: | --- |
| GET | `/` | 200 | HTML |
| GET | `/assets/app.js` | 200 | JavaScript |
| GET | `/assets/styles.css` | 200 | CSS |
| GET | `/api/recordings` | 200 | `recordings` |
| GET | `/api/sessions` | 200 | `sessions` |
| GET | `/api/session-dashboard?session_id=` | 200 | `session`, `workflow`, `asr`, `diarization`, `semantic`, `integrity`, `schema_version` |
| GET | `/api/session-evaluation?session_id=` | 200 | `available`, `can_create`, `reason`, `v2d1_run_id`, `review`, `truth`, `benchmarks` * |
| GET | `/api/dashboard?recording_id=` | 200 | `recording`, `segments`, `annotations`, `events`, `actions`, `evaluations`, `runs`, `last_run`, `stages`, `schema_version` |
| GET | `/api/evaluations?recording_id=` | 200 | `evaluations` |
| GET | `/api/evaluations/{recording_id}/{name}` | 200 | `metadata`, `segments` |
| GET | `/api/actions?recording_id=` | 200 | `actions` |
| GET | `/api/semantic?recording_id=` 或 `session_id=` | 200 | `available`, `recording_id`, `session_id`, `can_generate`, `reason`, `inputs`, `version`, `run`, `summary`, `privacy`, `transport`, `episodes`, `excluded_blocks`, `unresolved`, `reviewed_candidates`, `candidates` * |
| GET | `/api/runs?recording_id=` 或 `session_id=` | 200 | `runs` |
| GET | `/api/speaker-timeline?recording_id=` 或 `session_id=`；可选 `run_id` | 200 | `available`, `recording_id`, `session_id`, `duration_ms`, `reason`, `run`, `summary`, `speech_ms`, `speakers`, `queues`, `v2d1`, `v2d2`, `v2d3`, `manual_identity`, `method` * |
| GET | `/api/speaker-timeline/window`，目标、`run_id`、`start_ms`、`end_ms` | 200 | `recording_id`, `session_id`, `run_id`, `start_ms`, `end_ms`, `duration_ms`, `turns`, `overlaps`, `tokens`, `speech_evidence`, `source_regions`, `identity_regions`, `v2d2`, `audio_url` |
| GET | `/api/speaker-timeline/audio`，目标、`start_ms`、`end_ms` | 200 | WAV 二进制 |
| GET | `/api/jobs/{hex_job_id}` | 200 | `id`, `kind`, `recording_id`, `session_id`, `status`, `stage`, `detail`, `result`, `error` * |
| GET | `/api/audio/{segment_id}`；可选 `mode=segment|context` | 200 | WAV 二进制 |

### 4.2 写入

| 方法 | 路径 | 成功状态 | 响应顶层字段 |
| --- | --- | ---: | --- |
| POST | `/api/daily-run` | 202 | `id`, `recording_id`, `status`, `stage`, `detail`, `result`, `error` |
| POST | `/api/speaker-timeline/possible-review` | 200 | `candidate_id`, `status`, `note`, `updated_at`, `review` |
| POST | `/api/speaker-timeline/possible-identity` | 200 | `candidate_id`, `identity_label`, `updated_at`, `review` |
| POST | `/api/speaker-timeline/manual-identity` | 200 | `annotation`, `overview` |
| POST | `/api/speaker-timeline/manual-identity/retract` | 200 | `annotation`, `overview` |
| POST | `/api/speaker-timeline/possible-review/complete` | 200 | `run_id`, `candidate_count`, `reviewed_count`, `pending_count`, `status_counts`, `completed`, `completed_at`, `confirmed_speech_count`, `identity_labeled_count`, `identity_pending_count`, `identity_counts`, `items` |
| POST | `/api/session-evaluation/v2d1` | 200 | `available`, `truth`, `benchmarks` |
| POST | `/api/speaker-timeline/v2d2` | 200 | `run_id`, `truth`, `timeline` |
| POST | `/api/speaker-timeline/v2d3` | 202 | `id`, `kind`, `recording_id`, `session_id`, `status`, `stage`, `detail`, `result`, `error` |
| POST | `/api/speaker-timeline/identity-review` | 200 | `run_id`, `candidate_id`, `target_identity`, `start_ms`, `end_ms`, `status`, `note`, `updated_at`, `reference_set` |
| POST | `/api/semantic/generate` | 200 | `run_id`, `recording_id`, `session_id`, `asr_run_id`, `diarization_run_id`, `episode_count`, `excluded_block_count`, `llm_job_count`, `llm_payload_bytes`, `token_count`, `candidate_count`, `scene_count`, `claim_count`, `action_count`, `unresolved_count`, `request_sha256`, `provider_request_sha256`, `response_sha256` |
| POST | `/api/semantic/candidates/{candidate_id}/review` | 200 | `candidate_id`, `run_id`, `status`, `title`, `body`, `note`, `revision_index`, `created_at` |
| POST | `/api/evaluations/{recording_id}/{name}/run` | 200 | `evaluation_run_id`, `recording_id`, `item_count`, `metrics`, `report_markdown_path`, `report_json_path` |
| POST | `/api/actions/{candidate_id}/review` | 200 | `id`, `recording_id`, `type`, `status`, `title`, `scheduled_at`, `time_text`, `location`, `confidence`, `source_segment_ids`, `participants`, `evidence` |
| PUT | `/api/evaluations/{recording_id}/{name}/segments/{segment_id}` | 200 | `segment` |

## 5. 盲标工作台端点清单

盲标音频支持单一 byte range：合法 Range 返回 `206` 和 `Content-Range`；
越界返回 `416`。其他认证、同源和 JSON 错误规则与主工作台一致。

| 方法 | 路径 | 成功状态 | 响应类型 / 顶层字段 |
| --- | --- | ---: | --- |
| GET | `/` | 200 | HTML |
| GET | `/assets/blind.js` | 200 | JavaScript |
| GET | `/assets/blind.css` | 200 | CSS |
| GET | `/api/task` | 200 | `name`, `protocol`, `scope_start_ms`, `scope_end_ms`, `review_duration_ms`, `coverage_semantics`, `completeness`, `blind_attestation`, `speech_source_default`, `finalized`, `windows`, `utterances` |
| GET | `/audio/{window_index}` | 200 / 206 | WAV 二进制 |
| POST | `/api/utterances` | 200 | `utterance` |
| POST | `/api/utterances/{utterance_id}/delete` | 200 | `deleted` |
| POST | `/api/windows/{window_index}/status` | 200 | `window` |
| POST | `/api/finalize` | 200 | 与 `/api/task` 相同 |

## 6. Quality workflow 阶段矩阵

| 阶段 | 执行 / 复用条件 | 失败策略 | 对最终状态的影响 |
| --- | --- | --- | --- |
| 准入 | `production` 要求 production ready 并复核备份；`shadow` 要求 shadow ready | 终止；workflow run 写为 `failed`；模型不启动 | 无成功状态 |
| 原始输入完整性 | 清单为 `verified`/`not_applicable`，每个实例为 `verified`，且无 gap/overlap | 终止；workflow run 写为 `failed`；模型不启动 | 无成功状态 |
| ASR / V2-C | 只有非空 `model_signature` 才允许匹配；要求相同 session、`config_sha256`、input fingerprint。完成 run 直接复用，失败/运行中匹配交给 `resume_run_id` | 终止基础流程 | 成功后进入 diarization |
| Diarization / V2-D | 非空 `model_signature`；只复用完成 run；`asr_run_id` 和全部设置相同 | 终止基础流程 | 成功后进入增强阶段 |
| Speech recall / V2-D.1 | 只复用完成 run；D、C 父 run 和全部 D.1 设置相同 | 不终止；记录 `v2d1_failed`，要求人工复核 | 基础语义流程继续 |
| Identity audit / V2-D.2 | 无冻结 speaker truth 时跳过；否则只复用完成且 D run、truth set 相同的 run | 不终止；记录 `v2d2_failed`，要求人工复核 | 基础语义流程继续 |
| Identity mining / V2-D.3 | workflow 不自动运行；只读取同一 D run 的最新完成结果及人工审核覆盖层 | 无匹配 run 时按 D.2 结果标为 `needs_review` 或 `not_applicable` | 待审核候选产生 review reason |
| Semantic / V2-E.0.2 | 有 committed token 时运行；只复用完成的 `v2-e.0.2`、相同 C/D 父 run、`local_mock` provider 和相同设置 | 终止基础流程 | 成功为 `semantic_ready` |
| 空 semantic | 没有 committed token 时跳过，不调用 provider | 不是失败 | 成功为 `semantic_ready_empty` |
| 汇总 | 任一增强 review reason 存在时追加 `_needs_review`；已完成 D.1 人工审核可由覆盖层消解对应 reason | 持久化 summary 后完成 workflow run | `semantic_ready[_needs_review]` 或 `semantic_ready_empty[_needs_review]` |

当前 review reason 代码包括：

- V2-D.1：`possible_speech_high`、`unassigned_tokens_high`、`v2d1_failed`；
- V2-D.2：`identity_cluster_contamination`、`identity_truth_coverage_low`、
  `identity_fragmented`、`v2d2_failed`；
- V2-D.3：`v2d3_target_identity_required`、`v2d3_candidates_pending`。

## 7. 架构审查基线

`tests/test_architecture.py` 阻止新增以下债务，同时允许阶段 1 直接删除旧债：

- `services/` 中新增跨模块私有名称导入；
- 新增 service import cycle；
- 新增函数内部的 service import。

当前显式白名单只有：

1. `services/timeline.py` 从 `exporters.py` 导入 `_absolute_timestamp` 和
   `_group_segments`；
2. `session_backup` 与 `session_readiness` 的循环；
3. `session_readiness.evaluate_session_readiness` 内部导入
   `verify_session_backup`。

代码评审还必须检查：新增超大函数是否同时承担多个变化原因、是否修改既有
migration、是否改变 CLI/API 字段与退出码、是否引入模型/路径/数据库副作用。
阶段 1 应优先删除上述白名单，而不是扩大它。
