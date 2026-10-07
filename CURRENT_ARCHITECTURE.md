# Current V3 architecture

Status: current architecture after the 2026-10-07 engineering remediation. Validation evidence and limitations are recorded in `ENGINEERING_REMEDIATION_REPORT.md`.

## Contract and data ownership

Phone receipt metadata retains the Watch's raw session directory (for example
`pcm_session_1760000000000`) and source path for restart recovery. The Phone
manifest builder maps the directory timestamp to the canonical cross-device
`sessionKey` (for example `watch-session:1760000000000`). ASR ingest and
the session projection use that manifest key; replaying the same manifest is
idempotent and does not create a second logical session.

`contracts/v3/release-lock.json` freezes wire contract `3.8.0`, projection `5`, core contract schema `27` and Phone projection schema `19`. Internal SQLite migration v029 adds dirty date maintenance without changing the wire contract. ASR Core owns canonical recording, utterance, event and reminder state. Phone owns original local recording files and its rebuildable projection. This remediation has not altered contract payloads or production semantic data.

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

Migration v029 records dirty Singapore dates from session, transcript, processing, segment, manifest, event/evidence, summary and relevant speaker identity writes. It seeds only today/yesterday, never historical replay. Normal `DailyGenerationCoordinator.catch_up()` consumes at most 64 dirty rows per pass and at most 14 dates per range, plus today/yesterday; `run_once()` inventories recent or queued dates only. SQLite inventory queries are date bounded and expose row-count diagnostics. `full_reconcile()` is an explicit read-only repair/diagnostic path, never the background default. Historical backfill remains a separate, explicitly requested workflow.

## Model execution

Event, reminder and insight generation use `ModelExecutionRunner`. Production calls run in a read-only, exclusively owned subprocess with a 180-second call timeout and process-tree cancellation. A persisted job allows at most two explicit attempts, a 256 KB request and 150,000 reported tokens when usage is available. A persisted batch allows at most 16 calls and has a 30-minute deadline. Receipts record started/completed/timeout/failed/cancelled, retryability and available usage. The runner starts no retry or next batch automatically. Daily semantic generation retains its existing bounded attempt service. Synthetic tests use fake clients; they never contact a model.

## Model identity and development environment

The runtime selects an exact cached snapshot. A single snapshot is unambiguous; multiple snapshots require `ALLDAY_MODEL_SNAPSHOT_IDS` as a JSON map from model ID to snapshot directory name. Missing cache raises, unless `ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK=1` is explicitly set. Cache modification time is never a selection rule. Python is constrained to 3.12; `pyproject.toml` declares runtime dependencies and dev `pytest==8.4.2`, `ruff==0.16.5`. `requirements-dev.lock` is a pip-compatible Windows/Python 3.12 resolution: install it with `python -m pip install -r requirements-dev.lock`, then `python -m pip install -e . --no-deps`. Regenerate it with `uv pip compile pyproject.toml --extra dev --python-version 3.12 --python-platform windows --output-file requirements-dev.lock`. Torch and Torchaudio are pinned together at 2.9.1; GPU deployments may explicitly supply matching CUDA wheels.

## Current validation and remaining boundaries

The validation matrix is: complete ASR pytest and Ruff, Desktop `npm test` and build, Phone/Watch/common host tests and build, cross-repository contract verification, and synthetic fault/performance checks for 7, 90 and 365 days. Device-only behavior requires separate device evidence and is not inferred from host tests. See `ENGINEERING_REMEDIATION_REPORT.md` for this checkout's actual results and remaining boundaries.
