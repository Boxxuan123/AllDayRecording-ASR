# Speaker profile purity audit

Date: 2026-09-28. This document describes the code and protocol only. Identifiable names, audio ranges, embeddings, task snapshots, and review answers stay in the ignored `outputs/` and `state/` directories.

## Purpose and scope

An accepted prototype or a human person fact does not prove that every contributing clip is clean single-speaker speech. This audit records a new, clip-level evidence layer. It never updates person facts, prototype reviews, profile eligibility, thresholds, CAM++ weights, or production matching. A cluster spot-check confirms only the individually heard clips.

The generator uses the same eligibility predicate and policy/review/link conditions as `person_vectors()`. It traces each effective prototype through the current sample set, selected clip, source media range, overlapping person facts, source track, and cluster. Tasks deduplicate by the exact `(media_id, start_ms, end_ms)` source range across prototype and sample references. The frozen profile snapshot and individual task snapshot are stored with a version, model/version, embedding digest, and profile digest.

## First batch

The private run produced 169 distinct playable ranges: 129 P0, 14 P0-special, 20 P1, and 6 P2. The 14 special items include nine distinct failure-query ranges and five nearest active enrollment ranges. The 129 P0 plus five overlapping special enrollment ranges cover all 134 distinct active matching source ranges. P0 exceeding 50 is intentional: every effective source clip must be heard before concluding the profile is pure. All current effective known-person profiles are included, while the initial P1 ranking focuses on the historic confusion pair.

P1 uses the frozen V2 human-covered CAM++ window cache. It ranks similarity to the self reference and distance from the target reference, then proposes centroid, outlier, self-like, longest and seeded random windows by source cluster. This cache is a subset of all human facts; it is not an exhaustive embedding scan of every fact. P2 samples individual cached windows by person, date and session. Risk codes and original target identity stay server-side until a verdict is submitted. The private ranking file preserves the selection basis and this coverage limitation.

The historical failure's three predictions share original audio. They are one correlated event. Nearest-neighbor analysis compares each query's mean CAM++ vector against individually extracted vectors for every currently effective enrollment clip of the confused target. The full ordered similarities, not just the top five, are private in `failure-nearest-neighbors.json`.

## Mobile review

The existing review snapshot, paired-device action endpoint, audio renderer/cache/player, and person selector are reused. The inbox shows one `speaker_profile_purity` entry with the pending count. The dedicated small page asks only for the main speaker and then the target clip's purity; it hides the original target and risk reason. Optional other-speaker and quality choices follow. Target playback is exact. Context playback is server-bounded to at most four seconds on either side, and the response marks the target interval. The UI instructs the listener to judge only that interval.

Submitting appends a revision in `speaker_profile_purity_reviews`; it does not alter a production annotation. The pending item leaves the queue on the returned authoritative snapshot. Skip only moves a task to the end of the local pending queue and writes no verdict. Undo appends an `undo` revision and returns the task to pending. History remains playable and can append a corrected answer. A mismatch between chosen person and source target is flagged for later correction review, without applying a correction.

`clean_single`, `mixed_overlap`, `boundary_cross`, and `uncertain` are distinct. Only `primary_speaker_person_id == target_person_id` **and** `purity == clean_single` contributes to the offline clean pool. Matching identity alone never implies clean speech.

## Offline analysis after review

Run from the PC repository root:

```powershell
.\.venv\Scripts\python.exe tools\build_speaker_profile_purity_audit.py
.\.venv\Scripts\python.exe tools\analyze_speaker_profile_purity.py
```

The generator supports `--person`, `--priority`, `--limit` (P1 cap), `--dry-run`, and `--skip-embeddings`; it only inserts missing exact source ranges. The analyzer always writes progress, per-person and per-cluster reviewed counts, and a clean pool. Until all active profile sources have individual human verdicts, comparison outputs explicitly say `WAITING_FOR_HUMAN_REVIEW` and the confusion question is `INSUFFICIENT REVIEW` unless self audio has already been found.

Once complete, the analyzer rebuilds clean CAM++ vectors from only reviewed clean source clips and compares them with the current accepted vectors under the frozen V2 G/P gates and test split. No threshold is retuned. It saves controlled-group and original-track predictions, scores, margins, and failure-neighbor verdicts. This is retrospective: current accepted sources can postdate V2 test audio, so source overlap and timing must be checked before a causal production decision. The conclusion is A (contamination removed confusion), B (contamination remains insufficient), C (clean sources still confused), or inconclusive. The cluster table reports spot-check counts, never a population accuracy claim.

The failure queries receive their own human purity check. When a failed query contains the confused target's voice, the A pattern is labelled query-confounded rather than attributed solely to enrollment contamination. `clean-query-sensitivity.json` also scores only individually reviewed clean-self query windows under the same frozen gates. This deliberately changes query composition, so it is an exploratory sensitivity check and does not replace the frozen comparison.

## Sample worker risk

`annotation_sample_plan.py` groups effective active person facts by person and session. It rejects conflicting or excluded overlapping facts, unions contiguous ranges, breaks each union into up to five 8-second chunks, then selects the five longest eligible chunks (minimum 800 ms), with deterministic media/time ties. A short isolated 2% wrong-speaker portion may be filtered by length, but a long wrong-speaker portion can win selection, and unioning adjacent same-label facts can place a boundary error inside a selected chunk. The five-clip cap is per person/session sample set, not an independent purity guarantee. Its averaged prototype embedding can therefore absorb a small selected contamination. This audit measures that risk; it does not alter the selector.

## Verification and deployment note

Python compilation, Ruff, targeted persistence/review tests, the real database read path, and target/context audio rendering were checked. The HarmonyOS phone module built, and its host tests passed. No phone was attached to the build host at audit time, so navigation and taps on a physical device remain to be verified after installation. The PC service must run the updated code and the phone must install the new HAP, then refresh the review inbox while paired; persisted tasks alone do not update an already running old server process or an old phone binary.
