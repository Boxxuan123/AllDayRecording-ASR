# Self identity regression — 2026-10-01

The registered self voiceprint is present and loads successfully. The direct software regression is the non-learning early return introduced by `4e5479dfbaba9529e2fca0c05a4d657bb4240331`: reserving a session for Blind/holdout prevented product identity inference as well as learning. Real reserved sessions have automatic processing evidence but no product identity run before this fix.

A separate query issue matters: a reviewed self example contains sequential speakers. Its production track aggregate scores 0.1114 against the existing 0.1376 self threshold; one individual automatic turn scores 0.2526. Simply removing the return would still fail that aggregate. Single-window threshold acceptance also produces 3 false accepts in the preselected 12 historical negative windows. Neither threshold nor calibration was changed.

## Change

Non-learning `analyze` now runs a separate product self inference operation. It reads the existing accepted anchor and automatic ASR/diarization evidence. A complete utterance must be contained in one exclusive turn, have valid capture coverage, and have at least two disjoint windows of at least two seconds. Every evaluated window must agree. Short, mixed, incomplete, missing-anchor and uncertain queries remain unknown. Human identity corrections are preserved.

Product traces are stored in `speaker_cluster_runs` with producer `product-self-inference`. Normal utterance corrections publish the existing PC/Phone `identity` contract. This operation creates no cluster, prototype, profile, annotation truth or learning exposure. Frozen experiment rows and the existing learning pipeline remain separate. There is no schema migration or Phone change.

`tools/audit_self_identity_regression.py` reads a private manifest and emits read-only diagnostics with loaded anchor status, model fingerprints, source ranges, scores, other candidates, product traces, final PC identity and protected-table fingerprints. Private IDs, recordings, vectors and reports stay in ignored `outputs/`.

## Evidence and limits

- Anchor: 20 original source recordings, 82 references; accepted independent calibration with 25 positives and 101 negatives. Existing policy and enrollment digests remain valid.
- Fixed historical set: 12 self events from five sessions/four dates, and 12 negative events from twelve sessions/nine dates. Existing human-confirmed PC identities remain 12 self correct and zero negative-to-self before/after. These already-correct manual projections are not presented as newly recovered model accuracy.
- Diagnostic two-window replay on that fixed set: five self accepts, seven self unknowns, zero negative-to-self. Short windows remain unknown. This is a private diagnostic, not an official Blind score or an overall accuracy estimate.
- Two reviewed Blind V2 examples are diagnostic only. A product self utterance in the self example changes from unknown to self through the restarted production HTTP endpoint, with window scores 0.2551 and 0.1464. The reviewed negative example gets no product self identity. The clean subspan was selected programmatically from an exclusive turn; it has not received a separate human window-level confirmation.
- PC utterance HTTP projection confirms `identity=self`, preserving original `unknown` and its original evidence. No new upload or registration was performed. Phone rendering supports `self` already; the physical Phone screen was not independently observed: **NOT END-TO-END VERIFIED on Phone**.
- Protected-table before/after fingerprints match, including all Blind tables, reservation/learning exposure, annotations, prototypes/reviews, sample queue/sets, purity sources/candidates/profiles and identity policies. The original enrollment and model files are not edited.

## Validation

- Identity, enrollment, Blind V1/V2, review and speaker-turn suite: **116 passed, 1 skipped**.
- Entire current `tests` suite: **420 passed, 1 skipped, 2 pre-existing architecture failures**. The failures are the existing over-500-line source list and existing forbidden application imports. Baseline Git content confirms the same violations; this change adds none and does not relax those checks.
- Ruff, source compileall, changed test/tool py_compile, and `git diff --check`: passed.
- Bare pytest also discovers archived checkouts under ignored outputs and reports import collisions. The full current suite is therefore invoked explicitly as `pytest tests`; archived checkouts are preserved.
- Synthetic regressions cover explicit anchor loading, Blind/holdout inference, frozen prediction/profile isolation, rejection of non-learning enrollment, negative/short/mixed/missing/invalid queries, manual correction preservation, and restart persistence.
