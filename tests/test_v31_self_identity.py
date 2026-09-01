from __future__ import annotations

import unittest

from allday_asr.v3.domain import (
    IdentityHoldoutSample,
    SelfIdentity,
    VoiceprintCalibration,
    VoiceprintObservation,
    classify_voiceprint_identity,
    evaluate_voiceprint_calibration,
)


class V31SelfIdentityTests(unittest.TestCase):
    def test_uncalibrated_policy_fails_closed_even_for_high_scores(self) -> None:
        decision = classify_voiceprint_identity(
            (
                VoiceprintObservation(0.92, 3_000),
                VoiceprintObservation(0.91, 3_000),
            ),
            VoiceprintCalibration(
                policy_version="provisional-1",
                self_threshold=0.80,
                not_self_threshold=0.30,
                positive_holdout=2,
                negative_holdout=70,
                false_accept_rate=0,
                false_reject_rate=0,
                disjoint_holdout=True,
            ),
        )

        self.assertIs(decision.identity, SelfIdentity.UNKNOWN)
        self.assertFalse(decision.evidence["calibration_accepted"])
        self.assertIn(
            "insufficient_positive_holdout", decision.evidence["blocked_reasons"]
        )

    def test_clean_windows_use_strict_self_and_not_self_edges(self) -> None:
        calibration = VoiceprintCalibration(
            policy_version="holdout-accepted-1",
            self_threshold=0.80,
            not_self_threshold=0.30,
            positive_holdout=20,
            negative_holdout=100,
            false_accept_rate=0.01,
            false_reject_rate=0.10,
            disjoint_holdout=True,
        )

        self_decision = classify_voiceprint_identity(
            (
                VoiceprintObservation(0.91, 3_000),
                VoiceprintObservation(0.84, 2_500),
                VoiceprintObservation(0.99, 3_000, has_overlap=True),
            ),
            calibration,
        )
        not_self_decision = classify_voiceprint_identity(
            (
                VoiceprintObservation(0.20, 3_000),
                VoiceprintObservation(0.27, 2_500),
            ),
            calibration,
        )
        band_decision = classify_voiceprint_identity(
            (
                VoiceprintObservation(0.85, 3_000),
                VoiceprintObservation(0.70, 3_000),
            ),
            calibration,
        )

        self.assertIs(self_decision.identity, SelfIdentity.SELF)
        self.assertEqual(self_decision.evidence["excluded_window_count"], 1)
        self.assertIs(not_self_decision.identity, SelfIdentity.NOT_SELF)
        self.assertIs(band_decision.identity, SelfIdentity.UNKNOWN)

    def test_holdout_evaluation_records_far_frr_and_session_leakage(self) -> None:
        samples = tuple(
            [
                IdentityHoldoutSample(0.90, SelfIdentity.SELF, f"positive-{index}")
                for index in range(19)
            ]
            + [IdentityHoldoutSample(0.70, SelfIdentity.SELF, "enrollment-1")]
            + [
                IdentityHoldoutSample(0.20, SelfIdentity.NOT_SELF, f"negative-{index}")
                for index in range(99)
            ]
            + [IdentityHoldoutSample(0.85, SelfIdentity.NOT_SELF, "negative-fp")]
        )

        calibration = evaluate_voiceprint_calibration(
            samples,
            policy_version="evaluated-1",
            self_threshold=0.80,
            not_self_threshold=0.30,
            enrollment_session_ids=frozenset({"enrollment-1"}),
        )

        self.assertEqual(calibration.false_accept_rate, 0.01)
        self.assertEqual(calibration.false_reject_rate, 0.05)
        self.assertFalse(calibration.disjoint_holdout)


if __name__ == "__main__":
    unittest.main()
