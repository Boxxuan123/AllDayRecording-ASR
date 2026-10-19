# Memory core V2: isolated candidate
Base: ASR 0.8.28 / 3bf485ad16338b4b8892b3f3e0ac2ec17ff064cb.
Hindsight: v1.0.0 / 39c65ffe2f140d0c84cee58566cad8900c5b9908, MIT.
Phone: NO CHANGE. No production switch, master merge or phone installation.

Private runtime, snapshots, model receipts and evaluation standards stay under ignored outputs/.
The native service reports inserts only: update/delete/recall propagation is unsupported.
Application invalidation can only apply revisions and tombstones actually received.

Dependency map:
native read-only ChatData -> SourceRecord -> local SourceStore -> Hindsight adapter
natural question -> bounded model planner -> retrieve (Hindsight + raw projection)
-> grant/revision/span validation -> short query-local citations -> existing ModelExecutionRunner
overview -> same SourceStore / evidence / answer path; never writes generated summaries to memory.

Final execution results and limitations are recorded in REPORT-memory-core-v2.md.

Stage 2 execution:
- `query.py` executes model-produced source windows and inherits validated focus anchors.
- `selection.py` ranks continuous source fragments and packs bytes with shared actor headers.
- `MemoryCore.query` bounds the full warm query to 180 seconds; model calls remain <=90 seconds and <=4 per query.
- `answer` can request at most two grounded actions per round; repeated searches and invalid/stale anchors are rejected.
- `continuation status|run` uses a distinct 24-attempt / 400,000-token ledger. Persisted policy cannot be relaxed by another entry point.
- `raw` and `hybrid` component modes share planner output, source validation, fragment ranking and input limits.
- A valid quotation establishes provenance, not logical entailment. See the explicit synthetic counterexample and the report.

The final post-budget source changes have offline tests, not final-version semantic acceptance.
Evaluation fixtures and source windows are local caller inputs; no case answers or required IDs are imported by production modules.
