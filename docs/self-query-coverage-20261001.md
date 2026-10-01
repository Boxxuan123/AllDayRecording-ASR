# Self query coverage diagnosis

The fixed historical diagnostic cohort was reused without reselection: 12 self
events and 12 non-self events. The previous two-window diagnostic reproduced
5 self, 7 unknown, and zero non-self accepted as self. The model, enrollment,
normalization, calibration, thresholds and independent-window requirement stayed
fixed. This document contains no private event identifiers or audio hashes.

## Query construction defect

One unknown event has 8.96 seconds within a single automatically preserved
exclusive speaker turn, with no foreign regular turn crossing. Its source mapping
continues exactly across two capture files. The human person audio anchor covers
both source ranges; it is not a primary-person-only Blind review.

The old diagnostic manifest supplied only the first 2.46-second capture portion.
Separately, the product planner selected a token boundary 220 milliseconds before
that capture edge. It emitted the following 6.5-second continuation, but recorded
the short tail as `below_minimum_useful_duration`. Product inference rejected the
entire query because it had any planning exclusion.

The local product builder now replans without token boundaries only when every
exclusion is a short tail, after the existing whole-utterance exclusive-turn and
foreign-regular-turn checks have passed. It accepts the replacement only if the
same source planner reports no exclusions. Capture gaps, invalid mappings,
foreign turns, overlap, missing evidence and manual identity preservation remain
protected. The shared Blind planner and frozen predictions are unchanged.

The recovered disjoint windows are 2.46, 3.25 and 3.25 seconds. In candidate replay
their scores were approximately 0.3585, 0.4049 and 0.3859, above the unchanged
0.137604 self threshold. All three must pass; one positive window is insufficient.

## Remaining six unknown events

Five have confirmed event spans below four seconds. The sixth has a nominal
four-second anchor, but automatic regular-turn evidence marks 658 milliseconds
of foreign overlap. Its conservative available span is therefore at most 3.342
seconds, split into 1.027 and 2.315 seconds. It is not an established clean,
two-window matcher failure. The audio anchor alone does not independently prove
speaker composition or prove that the overlap detector is wrong.

Using the requested categories: A = 6, B = 1, C = 0, D = 0, E = 0 established.
Potential ownership errors require acoustic review; they are not inferred from
transcript identity. Oracle windows were not extended beyond confirmed audio
ranges to obtain a favorable score. Only the B event had sufficient confirmed
and automatically exclusive audio for a safe oracle comparison.

## Metric interpretation

The previous 5/7 metric is an offline first-capture diagnostic, not the current
production builder's accuracy. These historical events already have human
identities, and actual product queries preserve those identities.

| Measurement on the same fixed cohort | Before | After |
|---|---:|---:|
| Paired diagnostic self accepted | 5 | 6 |
| Paired diagnostic self unknown | 7 | 6 |
| Paired diagnostic non-self accepted as self | 0 | 0 |
| Actual builder self accepted on restored automatic projections | 1 | 2 |
| Actual builder self unknown on restored automatic projections | 11 | 10 |
| Actual builder non-self accepted as self | 0 | 0 |

The paired diagnostic retains every old query except the established B event,
which uses recovered product windows. The full builder replay instead applies
all current ownership gates to all 24 events, on an isolated copy of the
automatic projection. Human audio facts and frozen tables are preserved.
Neither measurement is an official Blind score. Live human projections remain
12 self and 12 non-self; they are not counted as new automatic corrections.

## Validation

Structural regression fixtures cover pure speech, 2.5-second speech, separated
turns with foreign speech, overlap, a clean turn inside a mixed track, window
disagreement, near-threshold negative windows and capture continuation. Additional
fixtures reject capture gaps and invalid mappings. Relevant tests passed:
133 passed, 1 skipped. Ruff, compileall, py_compile and diff checks passed.
The final full `pytest tests` run had 430 passed, 1 skipped and the same two
pre-existing architecture failures (source-length limits and forbidden adapter
imports). An initial Windows temporary-file replacement failure passed an
individual recheck and did not recur in the final full run.

Twenty-two protected table fingerprints stayed identical in production and the
isolated product replay. Enrollment, accepted policy and model file hashes stayed
identical. Actual product inference on original audio in the isolated database
accepted the recovered windows while retaining the existing self projection.
No diagnostic result was written to the production database.

The PC services were restarted to load the fix. Phone code is unchanged. A phone
was connected but its observed surface was the lock screen, so the actual synced
“本人” label remains NOT VERIFIED.

The next priority is a separately scoped short-speech matcher evaluation using
the existing 3.04- and 3.76-second non-overlapping cases and the fixed negatives.
Keep the production two-window rule during that study. Acoustic ownership review
is required before treating the nominal four-second overlap case as a clean
matcher failure. This cohort provides no clean two-window low-score evidence
justifying enrollment, profile or model changes.
