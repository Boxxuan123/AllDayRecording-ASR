# Current V3 architecture

Status: current architecture after the 2026-10-07 engineering refactor. Current validation evidence and limitations are in `PROJECT_ENGINEERING_REFACTOR_PROGRESS.md`; `ENGINEERING_REMEDIATION_REPORT.md` retains the preceding remediation's evidence.

## Contract and data ownership

Phone receipt metadata retains the Watch's raw session directory (for example
`pcm_session_1760000000000`) and source path for restart recovery. The Phone
manifest builder maps the directory timestamp to the canonical cross-device
`sessionKey` (for example `watch-session:1760000000000`). ASR ingest and
the session projection use that manifest key; replaying the same manifest is
idempotent and does not create a second logical session.

`contracts/v3/release-lock.json` freezes wire contract `3.8.0`, projection `5`, core contract schema `27` and Phone projection schema `19`. Internal migrations v029/v030 add dirty dates, sample queue age and annotated-person invalidation without changing the wire contract. The current private core schema is 30. ASR Core owns canonical recording, utterance, event and reminder state. Phone owns original local recording files and its rebuildable projection. This refactor has not altered contract payloads or production semantic data.

## Evidence replacement

Manual utterance correction and successful processing replacement call `application/evidence_replacement.py` within their existing unit of work. The rule invalidates dependent artifacts and automatic derived objects, queues recomputation, and preserves old utterance rows as provenance. Pending reminder candidates can become conflicted. A user accepted task keeps `derivation_status=active`; its immutable `derivation_dependencies.input_revision` is compared with the current utterance revision/status to derive the existing `source_review_required` response. The task is not recomputed, and evidence replacement issues no Calendar deletion. Automatic event acceptance cannot update an already user confirmed task. Daily Event and Summary continue to use their date and generation reconciliation without duplicate cascade invalidation.

`EvidenceProjectionRepository.supersede_previous_utterances` is the port for retiring old active revisions. The SQLite adapter selects prior input revisions and marks them stale. Repeated supersede finds no active old rows. A failure in invalidation rolls back the containing unit of work.

## Voice policy ownership

User controlled fields are `auto_match_enabled`, suggestion/accept thresholds, minimum margin and quality. Maturity status and calibration are machine computed. Maturity refresh calculates outside the writer transaction, then checks the policy revision inside the writer transaction. On conflict it recomputes against the latest user policy, with a bound of three attempts. It cannot commit fields read from a superseded revision.

## Ingest transaction boundary

`V3UploadIngestAdapter` parses the Phone manifest and stages content addressed manifest and audio files with hash verification and fsync before opening the metadata writer transaction. The transaction re-reads current session and manifest revision, checks idempotency and publishes references. Staged immutable content may remain after a failed metadata transaction and is safe for retry. Manifest protocol validation is in `adapters/transfer/manifest_validation.py`; device projection payloads are in `manifest_projection.py`.

## Dependency direction

Core/domain and application depend on focused repository ports. Current manifest revision, evidence supersession, annotation snapshots, review adoption and policy compare/update are expressed as business capabilities. SQLite SQL and inventory reads live in `adapters/sqlite`; annotation planning is pure domain code. The architecture gate forbids core/application imports of SQLite or adapter connections. Its file-size threshold is 600 lines after responsibility-based extraction of manifest validation and purity inventory.

## Daily incremental inventory

Migration v029 records dirty Singapore dates from session, transcript, processing, segment, manifest, event/evidence, summary and relevant speaker identity writes; v030 adds manually annotated person profile revisions. Startup seeds only today/yesterday. Sync commits source and dirty changes together, then only notifies the owner. `DailyGenerationCoordinator` exists with models disabled and runs local cache/invalidity maintenance without model calls. One pass reads at most 64 dirty rows and handles at most 14 unique dates total, including today/yesterday, acknowledging only the processed portion. Deleted source dates with a published summary remain in inventory so the result can be visibly stale.

Ordinary catch-up admits new remote jobs only for today/yesterday. Previously admitted durable jobs can resume. Historical dirty dates receive local maintenance; historical remote regeneration requires explicit backfill. `full_reconcile()` remains explicit and read-only. Daily input reads batch audio ranges, speaker references, person names and task evidence; their SQL count does not grow once per utterance. Queue decisions use classification/reason_code, independently of translated reason prose. The state transition table is in the progress report.

`daily_sync_projection.py` constructs the shared summary wire payload, omitting objective.events. Normal publication and invalidation publish through it. `mark_daily_stale` checks the planned content revision and commits invalidation plus a changed effective projection in one writer UOW. Equivalent latest payloads do not append twice; missing stale publication is repaired even when invalidation already exists. Publication failure rolls back invalidation and leaves dirty work pending. State changes retain the content revision and advance sync sequence; immutable bootstrap watermarks remain unchanged. Explicit daily-sync-report/daily-sync-repair commands inspect at most 14 specified Singapore dates and publish audited local repairs without regeneration or ordinary historical scans.

The Phone SQLite consumer accepts same-content-revision Daily state changes and fences old/duplicate daily_summary changes using the persisted cursor sequence. Cursor updates cannot regress; explicit projection rebuild resets the cursor inside its transaction. Incremental changes, restart, stale bootstrap, replay and failed cursor commit are exercised against the actual Phone repository and queries with SQLite. Phone schema remains 19; native OS APIs are mocked in this host test.

## Model execution

Event, reminder and insight generation use `ModelExecutionRunner`. Production calls run in a read-only, exclusively owned subprocess with a 180-second call timeout and process-tree cancellation. A persisted job allows at most two explicit attempts, a 256 KB request and 150,000 reported tokens when usage is available. A persisted batch allows at most 16 calls and has a 30-minute deadline. Receipts record started/completed/timeout/failed/cancelled, retryability and available usage. The runner starts no retry or next batch automatically. Daily semantic generation retains its existing bounded attempt service. Synthetic tests use fake clients; they never contact a model.

V2 execution identity includes task, entity, normalized source inputs, model/protocol/reasoning and an optional explicit regeneration ID. The first call persists its exact prompt and freezes now_utc before inference. Equivalent retries and restart reuse that identity and budget; new evidence or an explicit execution_id creates a distinct execution. Existing V1 receipt semantics remain available for direct legacy runner recovery; receipts are not rewritten or deleted.

Event/reminder request construction captures the actual complete input in a read snapshot. Version 1 input_snapshot is persisted in generation scope, checked within submit's writer UOW, and checked again in acceptance's publishing UOW. Changed text, usability or identity/task context rejects the old result. Manual commands capture expected source versions; reminder edits preserve the original candidate's validated snapshot. Old pending proposals without a trusted snapshot require regeneration. Insight/daily publication checks their full actual inputs, including uncited context. Public `project_evidence_identity` is shared by event/reminder/daily input builders.

Product rules instead use version 2, kind product-reminder: exactly the used utterance ID/revision/text/status, self identity, speech usability, capture times, session availability and timezone. Unused utterances and reminder context do not conflict. The shared dispatcher rejects unknown version/kind combinations; submit, desktop confirm/edit and Phone review use it in the publishing UOW. Model reminders retain complete version 1 context. Sequential same-generation creates are allowed only with persisted accepted proposal, candidate and matching unmodified create operation proof; external changes and target revision conflicts remain rejected.

Manual reminder edits keep a distinct generation/operation identity and the original input snapshot. Persisted revises_candidate_id links resolve candidate → proposal → generation to the original batch; pre-edit validation, replacement submission and final acceptance use the same trusted relationship checks. Caller-supplied batch IDs cannot grant an exemption. Missing/cyclic/mismatched links, different sessions/evidence, changed immutable operation fields and terminal ancestors are rejected. Final acceptance atomically publishes the replacement event/reminder/sync projection and marks all pending ancestors modified with feedback. Generic proposal acceptance without the reminder review callback rejects linked edits before any write.

A server-derived edit source key makes a traceable pending replacement reusable after a publication failure and process restart. Trusted older pending relationships can follow the same validation; missing historical proof is rejected without replacing its snapshot with current input. Accepted same-batch CREATE outputs remain strictly proven, including checking that cancellation/completion did not remove an accepted output from the active context. External changes still conflict and target revision CAS remains enforced.

ASR Phone reminder.review edit shares this source validation and atomic ancestor finalization. Proposal-local resolved-input revision checks use its own evidence while the original full model snapshot protects context. FUP-01B changes no Phone repository source, wire contract or migration. Current ASR review/receipt/device-sync tests and cross-repository contract checks passed; earlier Phone build/host-consumer results remain historical evidence.

GET /api/v3/reminder-candidates/{id}/product-recovery reports legacy rule recovery eligibility. Authenticated POST creates an explicit linked attempt, audits it and preserves the original history. A deterministic replacement and source-key redirect make recovery replayable. Missing historical proof, changed sources and existing user decisions do not qualify. No startup upgrade rewrites legacy pending or conflict snapshots.

## Recovery, native execution and health

Automatic workflow v2 state and retry outbox live in a private `automation/workflow-state.sqlite3`. Acceptance is transactional; repeated clicks share a request ID. Reading pending requests is replayable. Durable queued state and scheduling acknowledgement commit together, before memory enqueue. Requests for an active session stay pending. Legacy JSON is imported idempotently and retained for diagnosis; SQLite is authoritative after import.

The workflow sidecar now has its own schema version 3. Original status/request JSON and per-source import markers commit atomically; repeated initialization never derives a legacy request from live SQLite state. Orphan request files are supported. Scheduling claims use writer-transaction CAS across processes and persist their queued snapshot with the ACK. Explicit retry-report/retry-coalesce commands preserve all requests and audit only provably redundant, unclaimed legacy/UUID pairs. Legitimate subsequent retries retain distinct identities. Deployment must coordinate receiver/desktop upgrades; production historical coalescing has not been run.

Blind report exports, the device credential store and TLS PEM publication reuse the two-second bounded atomic-replace retry for Windows errors 5/32. Persistent failure still surfaces and preserves the prior publication; truth, isolation and authentication rules are unchanged.

Production native stages use an exclusive Windows Job child process. Backend construction, model loading, audio preparation, ASR windows, diarization, serialization and backend close occur inside that owned process; only the parent publishes business state through its fenced lease. Stage budget is min(24h, 900 seconds + 12 times recording duration); ASR progress budget is 900 seconds + 12 times window/context duration, and other stages use 900 seconds + 6 times recording duration, capped by the stage budget. Periodic lease renewal alone never resets progress deadlines. Cancellation/shutdown terminate only the owned Job and wait for all of its members. Shutdown is retryable and no returned result can publish after close.

Annotation sampling renews its 120-second lease every 30 seconds, constrained by token, generation, unexpired lease, close and a one-hour maximum run. Renewal starts before snapshot/planning. Source revision and provider identity are checked again with sample publication. Three exhausted attempts are visible as failed; infrastructure iteration failures stop after three consecutive errors. An uncooperative embedding provider may outlive the two-second worker join, but renewal stops and late publication is fenced; health exposes shutdown_incomplete. Real embedding/GPU teardown needs device validation.

HTTP errors carry request_id; sanitized diagnostics keep exception types, chained stack frame locations and applicable stable IDs, without exception messages or source-code literals. Daily errors distinguish source conflict, invalid input, rejected provider output, retryable provider failure, budget termination, cancellation and programming/storage failure. `GET /api/v3/worker-health` exposes Daily and sampling state, last success/error and backlog; receiver automation also exposes its health method.

The recompute ledger reports manual execution mode and the required regeneration action. Semantic generation can update an existing stale event identity with a revision CAS. No generic automatic recompute consumer or implicit historical model replay is promised. Confirmed task intent remains protected.

## Desktop query lifecycle

The review inbox loads its main list and candidate details separately. Auxiliary failure retains the list and exposes a local retry; decisions requiring missing candidates are disabled. Pagination guards late responses. Successful evidence/review changes invalidate related overview, recordings/timelines, people and review caches. Shared queries retain subscriber cancellation and version guards, with 30-second TTL cleanup and a 128-entry cache cap. Actual Vue component and query lifecycle tests cover these behaviors.

## Model identity and development environment

The runtime selects an exact cached snapshot. A single snapshot is unambiguous; multiple snapshots require `ALLDAY_MODEL_SNAPSHOT_IDS` as a JSON map from model ID to snapshot directory name. Missing cache raises, unless `ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK=1` is explicitly set. Cache modification time is never a selection rule. Python is constrained to 3.12; `pyproject.toml` declares runtime dependencies and dev `pytest==8.4.2`, `ruff==0.16.5`. `requirements-dev.lock` is a pip-compatible Windows/Python 3.12 resolution: install it with `python -m pip install -r requirements-dev.lock`, then `python -m pip install -e . --no-deps`. Regenerate it with `uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform windows --output-file requirements-dev.lock`. Torch and Torchaudio are pinned together at 2.9.1; GPU deployments may explicitly supply matching CUDA wheels.

## Current validation and remaining boundaries

The repeatable current gate is `tools/validate_engineering_refactor.py --phone <Phone checkout>`, covering complete ASR pytest/Ruff, cross-repository contract verification, Desktop tests/vue-tsc/build. It writes fresh per-command logs, exit codes and pytest XML. Synthetic regressions include real composition/sync with 7/90/365 days of utterances, people and confirmed tasks, fault recovery, owned process termination and fake-clock sampling. Phone consumer changes have an actual SQLite integration test driven by `tools/check_phone_daily_sync.py`, a debug project build and the existing 170-test host gate. Device-only behavior and real remote/GPU/model quality remain separate NOT_RUN boundaries. See `PROJECT_ENGINEERING_REFACTOR_PROGRESS.md` for actual commands, results and compatibility/rollback limits.

## 2026-10-11 聊天事项手机接入

契约 3.9.0 / 投影 6 / Core 33 / Phone 20。聊天事项使用现有 Event、人工操作、同步 outbox 和回执；chat_record 证据独立于录音身份。首次提醒由用户确认执行时间后进入现有 ReminderSchedule 与 Calendar Kit 专用日历。新进展保存同一 Event 的待审核变化，人工标题／状态受保护。本轮跨 UI、Transport、Storage，既有超 500 行 ViewModel／Repository 复审与验证结果见 [验收记录](docs/CHAT_FOLLOWUP_PHONE_ACCEPTANCE_20261011.md)。代码接入完成；实机 NOT_RUN。
