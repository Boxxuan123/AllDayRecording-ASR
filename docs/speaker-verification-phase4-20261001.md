# Speaker Verification Phase 4 — 2026-10-01

CAM++ companion-dependent inference was confirmed and repaired without changing
enrollment, references, calibration, thresholds, model files or the production
two-window decision rule. Prospective reservations are active. Short-speech
matcher confusion remains; no short-speech candidate or new model is promoted.

Private audio, embeddings, source identifiers, event manifests and complete
diagnostics stay in ignored `outputs/speaker-phase4/`. This document contains
aggregate results and public regression case numbers only.

## Batch invariance

The fixed corpus contains 14 historical event targets, including Self duration
controls, anchors #1/#5, historical negatives #14/#15/#16 and the 2.56s strong
negative. It also contains source probes from all 20 original enrollment files:
34 targets, each tested alone, with a short companion, with a long companion,
and with mixed companions at batch sizes 4 and 8 (136 comparisons).

Normalized float32 mono 16 kHz target bytes, valid fbank feature hashes, model
files and references are identical across companions. Logs retain durations,
valid lengths, padded frame counts, embeddings/norms, cosines, self scores,
nearest eligible known-person similarities, decisions and cross-threshold flips.
These known-person similarities use the existing filtered production library;
they are diagnostic, not a newly calibrated decision margin.

| Metric | CPU before | CPU after | CUDA before | CUDA after |
|---|---:|---:|---:|---:|
| Maximum embedding drift, `1-cos(b1,bN)` | 0.418689132 | 0.000000119 | 0.418501318 | 0.000000119 |
| Maximum absolute self-score difference | 0.175954881 | 0 | 0.175794350 | 0 |
| Decision flips | 15 | 0 | 15 | 0 |
| Self-threshold crossings | 15 | 0 | 15 | 0 |

Actual FunASR 1.4.4 call-chain inspection established the cause:

1. `funasr/models/campplus/utils.py::extract_feature` computes and mean-centers
   each waveform's valid Kaldi fbank, then `pad_list` adds zero frames.
2. `campplus/model.py::inference` obtains feature lengths, but calls
   `self.forward(speech)` without them. Its forward API does not accept a valid
   length/mask.
3. CAM temporal attention and `components.py::StatsPool/statistics_pooling`
   operate over the full time axis, including padding. Valid fbank hashes remain
   identical, localizing the observed change after feature extraction/collation.
4. Our adapter's later L2 normalization cannot undo this change.

The minimal repair retains `extract_speaker_embeddings(..., batch_size=...)`
caller compatibility while always asking the existing FunASR generator for
`batch_size=1`. Protocol identifier: `cam++-single-waveform-v2`. No guessed
pooling mask, model modification, duration bucketing or diarization refactor was
introduced. CUDA legacy behavior was replayed offline through the old generator
API; the CPU before audit ran before the production adapter edit.

Reference limitation: NPZ metadata does not retain the exact timestamps of its
82 original reference windows. The 20 original-file probes therefore establish
source invariance, not a regenerated 82-vector reference library. The existing
82 vectors, NPZ and originals remain byte-identical.

## Historical re-evaluation

The original threshold is still **0.13760416209697726**.

| Case | Before b8, mixed | Before b1 | After b1 | After requested b8 |
|---|---:|---:|---:|---:|
| #14 | 0.142152742 | 0.118070514 | 0.118070514 | 0.118070514 |
| #15 | 0.138488669 | 0.027992394 | 0.027992394 | 0.027992394 |
| #16 | 0.168923482 | 0.005690843 | 0.005690843 | 0.005690843 |
| 2.56s strong negative | 0.431767204 | 0.410948083 | 0.410948083 | 0.410948083 |

The three historical false Self decisions disappear under deterministic
inference: engineering artifacts. The strong negative remains well above
threshold: **NOT EXPLAINED BY BATCH PADDING**. Genuine short-speech matcher
confusion remains. Anchor scores after repair: #1 0.387691233, #5 0.322853938.

The frozen 237-event cohort was replayed into a new private directory. Compared
with its previous controlled-b1 scores, all 237 full-event scores are identical
and decision flips are zero. Its previous candidate rules are copied unchanged:
SHA256 `d823478e0bcc7e2ab5ef9e8a52b1786c89dba6fc6a5fb9bbf32907c20582e14d`.
No old holdout was refitted and no old prediction file was overwritten. This is
historical engineering regression, not a fresh independent evaluation.

Deterministic 2–3s full-event distributions:

| Partition / truth | Events | Minimum | Median | Maximum | Above original threshold |
|---|---:|---:|---:|---:|---:|
| Development / Self | 11 | 0.011106 | 0.236074 | 0.359256 | 9 |
| Development / negative | 9 | -0.094448 | -0.009567 | 0.118609 | 0 |
| Old holdout / Self | 10 | 0.065455 | 0.270027 | 0.400592 | 9 |
| Old holdout / negative | 8 | -0.058682 | 0.053735 | 0.410948 | 3 |
| Historical reference / Self | 6 | 0.215808 | 0.345442 | 0.403528 | 6 |
| Historical reference / negative | 9 | -0.082680 | 0.020778 | 0.094235 | 0 |

The old holdout's 2–4s single-window candidate still accepts 3/13 negatives;
current two-window inference accepts none of those 13 and leaves all 22 Self
events Unknown. Across its full 41 Self / 41 negative events, the unchanged
two-window rule accepts 6 Self and one 4–6s negative. These are historical results
and cannot establish independent production safety.

Fixed-24 paired replay changes from 6 Self / 6 Unknown / 0 false Self to
5 / 7 / 0. Replay respecting actual utterance ownership remains 2 / 10 / 0.
The coverage reduction is reported, not offset by changing the threshold.

## Prospective independent reservation

Additive PC migration v25 activated at **2026-10-01T11:48:21.284Z**. Existing
sessions (cutoff rowid 26) are untouched. Eligible sessions must be ingested after
activation and have a UTC capture date strictly after the activation UTC date;
the entire activation day is excluded to avoid sharing previously used audio.
First eligible capture time: **2026-10-02T00:00:00Z**.

Frozen policy `utc-calendar-speaker-v1` groups whole UTC capture dates. Let
`d = days_since_1970_01_01 % 5`: day 0 is `independent_evaluation`, day 4 is
`development`, and the other days retain the prior learning/Blind/holdout
allocation. First independent date: **2026-10-04 UTC**. Allocation uses no model
score or review outcome; every session on a reserved date shares the same role.

Research role is explicit in `session_speaker_reservations`, with capture date,
reservation timestamp, first prediction timestamp and policy version. For
compatibility, development/evaluation sessions receive existing base `holdout`
before existing learning or Blind consumers see them. The current legacy Blind
collection setting remains unchanged; reserved research dates take precedence
for future sessions only. No old Blind role, query, snapshot or result changes.

Reservation occurs atomically in ingest. The first identity attempt is recorded
before provider/matcher invocation, using actual UTC time with strictly
`reserved_at < first_prediction_at`. Roles, capture dates, reservation fields,
first prediction and usage history are immutable. Accepted profile source clips
cannot be updated to introduce reserved audio.

Independent sessions allow normal product inference, frozen predictions, human
truth and final evaluation. Enrollment, profile learning, calibration, fitting,
candidate design, model selection, development, diagnostics and training are
blocked. They cannot later become learning. Base holdout guards protect the
existing enrollment/sample/profile paths; additional research guards prevent
learning exposure and prohibited purpose records.

`speaker_event_provenance(utterance_id)` returns role, reservation, enrollment,
calibration, profile, development, Blind and diagnostic flags, and independent
eligibility. Historical calibration provenance missing from metadata remains
unknown, rather than being asserted clean. Original enrollment SHA metadata
detects reimported enrollment sources. Historical diagnostic use is recorded
additively, never used to relabel old sessions as independent.

Isolated tests prove reservation before actual product inference, normal manual
review and product projection, and unchanged enrollment/profile/calibration
eligibility/sample queues/learning exposure. UTC-date and role repurposing fail.

## Production continuity

The requirement for two disjoint >=2s windows, all agreeing on Self, is unchanged.
Insufficient short-speech evidence stays Unknown. Candidate threshold, margin,
crop stability and enrollment consensus rules remain offline/shadow only.

Provider, matcher and matcher-status exceptions now degrade product identity to
Unknown. Automatic workflow catches broader speaker-stage failures after durable
ASR and continues downstream. It preserves trusted/manual existing identities.
No UI, task-assignment rule, event extraction rule, summary rule or relationship
rule was redesigned.

Regression tests establish transcript/timeline preservation, unresolved ownership
and pending todo candidates, anonymous events in daily-summary input, and no
relationship memory from unresolved identity. Product inference continues on
independent sessions without learning.

Both existing PC services were restarted after verifying no running/queued work.
Web UI and data-health return HTTP 200; the TLS receiver returns the expected
401 without device credentials. Protected fingerprints remain unchanged.

## Future model benchmark

**NO MODEL BENCHMARK RUN YET. NO NEW MODEL DOWNLOAD.** Current prospective
independent sessions/events: 0; startup gate is not met.

`tools/speaker_benchmark_protocol.py` exposes a small backend contract:
`embed(audio, valid_length)`, `embed_batch(inputs, batch_size=...)`, `metadata()`.
CAM++ uses the same production adapter. Metadata includes version, dimension,
model file hashes and preprocessing/inference protocol; missing cache is rejected
before any implicit download.

The manifest records event/utterance/session/date, reserved role/timestamps,
duration, original media/SHA/storage/ranges, normalized audio SHA, human fact IDs
and truth timestamp, purity/overlap/mapping/exclusions and all contamination
flags. The read-only readiness tool binds supplied future manifests to actual
SQLite reservations, active human truth, original-audio anchors/replicas and
reviewed clean-single source purity. It emits the contract and startup gate
without loading a model or exporting historical truth.

Minimum startup: >=30 independent 2–4s events, >=10 Self and >=10 negatives,
>=3 sessions and >=3 dates. Each truth needs >=2 sessions/dates and both short
bins. Both truths need 4–6s and >=6s controls. No session may supply more than
half of short events and no truth more than 80%. Contaminated, incomplete or
overlapping source events fail admission. This gate does not authorize promotion.

`fit_and_freeze` fits each model only on reserved development events using the
predeclared `max(development_negative_score) + 0.02` method; this method is for
future benchmark studies, never current production recalibration. Development
and independent evaluation must be session/date/source disjoint. The same
evaluation audio, facts, split and exclusions are frozen for every adapter.

Development batch invariance at 1, 2 (short/long), 4 and 8 is a hard prerequisite:
embedding drift and score drift <=1e-5, zero decision flips. Failure is
`ENGINEERING INVALID FOR BENCHMARK`, suppresses accuracy metrics and prevents
evaluation audio access. `evaluate_once` verifies frozen manifests, plan, model
and references and creates an exclusive attempt marker before evaluation audio
access. Failed runs also consume that study. No refit or winner selection from
evaluation is allowed.

Outputs include Self accepted/Unknown, negative→Self/rejected, Unknown coverage,
Self coverage, false accept and false reject overall and by duration. Synthetic
unit tests validate this protocol; they are not a comparison of real models.

## Integrity, validation and performance

Before/after hashes match for 29 protected historical table categories, including
Blind snapshot/predictions/truth/reviews/queries, prototypes/profiles/policies,
annotation truth, enrollment provenance sources, samples and learning exposure.
All 20 enrollment originals, 82x192 references/NPZ, production policy and
calibration, CAM++ files and FunASR 1.4.4 remain unchanged. Old diagnostic JSON,
candidate rules, manifests and predictions remain unchanged. Only additive
reservation/provenance metadata and new private audit artifacts were written.

PC SQLite is v25. Its new research metadata is explicitly PC-private in the
migration descriptor; the frozen distributed contract remains core v24,
projection 4, version 3.7.1. Exact canonical contracts and Phone receipts pass
read-only verification. **Phone NO CHANGE** at
`4245f99faafa257d6260d891a1e6e7d8c15c3549`.

Validation: related regressions 209 passed / 1 skipped; final benchmark protocol
13 passed; migration/contract/protocol release checks 28 passed. Final full
`pytest tests`: **478 passed, 1 skipped, 2 failed**. The two failures are the
existing architecture checks (seven already oversized production files and seven
existing application import violations). No new oversized production file or
forbidden application import was added. The existing automation file grows by
one line. Ruff passes; compileall and py_compile pass for 365 Python source
files; diff whitespace checks pass. Temporary backup directories are excluded
from source compilation.

Warmed inference workload: 237 real verification clips / 872.091 audio seconds,
unchanged fbank/model/normalization included; one-time source decoding excluded.

| Device | Protocol | Total seconds | Clips/s | Amortized ms/clip | Audio real-time factor |
|---|---|---:|---:|---:|---:|
| CPU | prior b8 | 7.208 | 32.878 | 30.415 | 0.00827 |
| CPU | fixed b1 | 13.486 | 17.573 | 56.904 | 0.01546 |
| RTX 5070 Laptop | prior b8 | 1.829 | 129.579 | 7.717 | 0.00210 |
| RTX 5070 Laptop | fixed b1 | 9.259 | 25.598 | 39.066 | 0.01062 |

Fixed CPU mean process utilization: 506.50% across cores; host 44.37%. Fixed CUDA
mean GPU utilization: 18.56%, peak 55%; mean process CPU 475.77%, host 49.65%.
GPU monitoring is device-wide and may include other applications. Prior CUDA
mean GPU utilization was 25.50%.

CPU inference slows 1.87x; CUDA slows 5.06x. The measured verification workload
still runs at about 65x/94x audio real time and fits the current offline need.
This is not an end-to-end ASR latency claim or a per-request tail-latency SLA.
Future throughput work needs correct model length/mask semantics and must retain
the invariance hard gate; approximate length bucketing alone is not a fix.
