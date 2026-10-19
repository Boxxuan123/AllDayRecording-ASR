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
