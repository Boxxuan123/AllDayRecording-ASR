from __future__ import annotations

import unittest

from allday_asr.v3.domain.reviews import (
    ReviewDisposition,
    event_proposal_review_disposition,
    person_memory_review_disposition,
    voice_review_disposition,
)


class V3ReviewPolicyTests(unittest.TestCase):
    def test_only_durable_unconfirmed_person_memory_requires_review(self) -> None:
        durable = {
            "kind": "stable_fact",
            "status": "active",
            "confirmation_status": "unconfirmed",
            "confidence": 0.82,
            "event_id": "event-1",
            "evidence_count": 1,
        }
        self.assertEqual(
            person_memory_review_disposition(durable),
            ReviewDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            person_memory_review_disposition({**durable, "kind": "plan"}),
            ReviewDisposition.SUPPRESS,
        )
        self.assertEqual(
            person_memory_review_disposition(
                {**durable, "confirmation_status": "inferred"}
            ),
            ReviewDisposition.SUPPRESS,
        )
        self.assertEqual(
            person_memory_review_disposition({**durable, "confidence": 0.40}),
            ReviewDisposition.SUPPRESS,
        )

    def test_event_layer_only_surfaces_trusted_memory_decisions(self) -> None:
        decision = {
            "operation": "create",
            "event_kind": "decision",
            "patch": {"summary": "改乘地铁", "confidence": 0.78},
        }
        self.assertEqual(
            event_proposal_review_disposition(decision, evidence_count=1),
            ReviewDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            event_proposal_review_disposition(
                {**decision, "event_kind": "important_experience"},
                evidence_count=1,
            ),
            ReviewDisposition.SUPPRESS,
        )
        self.assertEqual(
            event_proposal_review_disposition(
                {**decision, "patch": {"confidence": 0.45}}, evidence_count=1
            ),
            ReviewDisposition.SUPPRESS,
        )
        self.assertEqual(
            event_proposal_review_disposition(
                {**decision, "event_kind": "person_fact"}, evidence_count=1
            ),
            ReviewDisposition.SUPPRESS,
        )

    def test_optional_voice_learning_stays_out_of_required_inbox(self) -> None:
        self.assertEqual(
            voice_review_disposition({"decision_tier": "suggested"}),
            ReviewDisposition.REVIEW_REQUIRED,
        )
        self.assertEqual(
            voice_review_disposition(
                {
                    "decision_tier": "no_known_match",
                    "best_score": 0.75,
                    "score_margin": 0.15,
                }
            ),
            ReviewDisposition.OPTIONAL_LEARNING,
        )
        self.assertEqual(
            voice_review_disposition(
                {
                    "decision_tier": "no_known_match",
                    "best_score": 0.40,
                    "score_margin": 0.02,
                }
            ),
            ReviewDisposition.SUPPRESS,
        )


if __name__ == "__main__":
    unittest.main()
