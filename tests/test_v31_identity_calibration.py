from __future__ import annotations

import unittest

from allday_asr.v3.application.identity_calibration import (
    CalibrationWindow,
    calculate_threshold_metrics,
    merge_calibration_windows,
    select_not_self_threshold,
    select_self_threshold,
    split_adaptation_windows,
)
from allday_asr.v3.domain.identity import SelfIdentity


class V31IdentityCalibrationTests(unittest.TestCase):
    def test_adaptation_session_can_be_removed_as_one_group(self) -> None:
        windows = [
            CalibrationWindow(
                sample_id="adapt-self",
                session_id=1,
                session_key="recording:1",
                start_ms=0,
                end_ms=3_000,
                identity=SelfIdentity.SELF,
                origin="test",
                provenance=(),
            ),
            CalibrationWindow(
                sample_id="adapt-negative",
                session_id=1,
                session_key="recording:1",
                start_ms=4_000,
                end_ms=7_000,
                identity=SelfIdentity.NOT_SELF,
                origin="test",
                provenance=(),
            ),
            CalibrationWindow(
                sample_id="holdout-self",
                session_id=2,
                session_key="recording:2",
                start_ms=0,
                end_ms=3_000,
                identity=SelfIdentity.SELF,
                origin="test",
                provenance=(),
            ),
        ]

        adaptation, holdout = split_adaptation_windows(windows, frozenset({1}))

        self.assertEqual([value.sample_id for value in adaptation], ["adapt-self"])
        self.assertEqual([value.sample_id for value in holdout], ["holdout-self"])

    def test_temporary_labels_extend_legacy_without_overlap_or_media(self) -> None:
        bundle = {
            "sessions": [{"session_id": 1, "session_key": "recording:1"}],
            "effective_identity_windows": [
                {
                    "window_id": "old-self",
                    "session_id": 1,
                    "start_ms": 1_000,
                    "end_ms": 4_000,
                    "identity": "self",
                    "provenance_record_ids": ["old:1"],
                }
            ],
        }
        progress = {
            "identity_candidates": [
                {
                    "candidate_id": "overlap",
                    "session_id": 1,
                    "start_ms": 3_500,
                    "end_ms": 6_500,
                },
                {
                    "candidate_id": "new-negative",
                    "session_id": 2,
                    "session_key": "recording:2",
                    "start_ms": 10_000,
                    "end_ms": 13_000,
                },
                {
                    "candidate_id": "media",
                    "session_id": 2,
                    "session_key": "recording:2",
                    "start_ms": 20_000,
                    "end_ms": 23_000,
                },
            ],
            "identity_annotations": {
                "overlap": {
                    "label": "self",
                    "start_ms": 3_500,
                    "end_ms": 6_500,
                },
                "new-negative": {
                    "label": "mother",
                    "start_ms": 10_000,
                    "end_ms": 13_000,
                },
                "media": {
                    "label": "media",
                    "start_ms": 20_000,
                    "end_ms": 23_000,
                },
            },
        }

        windows = merge_calibration_windows(bundle, progress)

        self.assertEqual([value.sample_id for value in windows], ["old-self", "temporary:new-negative"])
        self.assertEqual(windows[1].identity.value, "not_self")

    def test_thresholds_enforce_far_and_keep_a_non_self_boundary(self) -> None:
        positives = [0.82, 0.88, 0.91]
        negatives = [0.20, 0.31, 0.45, 0.63]

        self_threshold = select_self_threshold(
            positives,
            negatives,
            max_false_accept_rate=0.0,
        )
        not_self_threshold = select_not_self_threshold(
            positives,
            self_threshold=self_threshold,
        )
        metrics = calculate_threshold_metrics(
            positives,
            negatives,
            self_threshold=self_threshold,
            not_self_threshold=not_self_threshold,
        )

        self.assertGreater(self_threshold, max(negatives))
        self.assertLess(not_self_threshold, min(positives))
        self.assertEqual(metrics.false_accept_rate, 0.0)
        self.assertEqual(metrics.false_reject_rate, 0.0)
        self.assertEqual(metrics.auc, 1.0)


if __name__ == "__main__":
    unittest.main()
