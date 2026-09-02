from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from allday_asr.v3.domain.identity import IdentityDecision, SelfIdentity
from allday_asr.v3.domain.people import SpeakerEmbedding


class CalibratedSelfIdentityMatcher:
    """Use the accepted V3.1 calibration policy and its bound CAM++ voiceprint."""

    def __init__(
        self, state_dir: Path, *, minimum_quality: float = 2_000 / 12_000
    ) -> None:
        if not 0 <= minimum_quality <= 1:
            raise ValueError("self identity minimum quality is invalid")
        self._state_dir = state_dir.resolve()
        self._policy_path = self._state_dir / "identity" / "active-self-identity-policy.json"
        self._minimum_quality = minimum_quality

    def status(self) -> dict[str, Any]:
        loaded, reason = self._load()
        if loaded is None:
            return {
                "available": False,
                "auto_identity_enabled": False,
                "reference_count": 0,
                "policy_version": None,
                "reason": reason,
            }
        policy, references, _ = loaded
        return {
            "available": True,
            "auto_identity_enabled": True,
            "reference_count": int(references.shape[0]),
            "policy_version": str(policy["policy_version"]),
            "self_threshold": float(policy["self_threshold"]),
            "not_self_threshold": float(policy["not_self_threshold"]),
            "false_accept_rate": float(policy["false_accept_rate"]),
            "false_reject_rate": float(policy["false_reject_rate"]),
            "reason": "accepted_calibrated_voiceprint",
        }

    def match(self, embedding: SpeakerEmbedding) -> IdentityDecision:
        loaded, reason = self._load()
        if loaded is None:
            return _unknown(reason)
        policy, references, centroid = loaded
        if "cam++" not in embedding.model.lower():
            return _unknown("incompatible_speaker_model")
        vector = np.asarray(embedding.vector, dtype=np.float32)
        if vector.ndim != 1 or vector.shape[0] != references.shape[1]:
            return _unknown("incompatible_embedding_dimensions")
        norm = float(np.linalg.norm(vector))
        if norm == 0:
            return _unknown("zero_norm_embedding")
        vector /= norm
        reference_scores = references @ vector
        top_k = min(3, references.shape[0])
        top_reference_score = float(
            np.partition(reference_scores, -top_k)[-top_k:].mean()
        )
        centroid_score = float(centroid @ vector)
        score = 0.7 * centroid_score + 0.3 * top_reference_score
        self_threshold = float(policy["self_threshold"])
        not_self_threshold = float(policy["not_self_threshold"])
        if embedding.quality_score < self._minimum_quality:
            identity = SelfIdentity.UNKNOWN
            decision_reason = "insufficient_track_quality"
        elif score >= self_threshold:
            identity = SelfIdentity.SELF
            decision_reason = "calibrated_score_above_self_threshold"
        elif score <= not_self_threshold:
            identity = SelfIdentity.NOT_SELF
            decision_reason = "calibrated_score_below_not_self_threshold"
        else:
            identity = SelfIdentity.UNKNOWN
            decision_reason = "calibrated_score_in_unknown_band"
        return IdentityDecision(
            identity,
            {
                "source": "calibrated_self_voiceprint",
                "decision": identity.value,
                "reason": decision_reason,
                "score": score,
                "centroid_score": centroid_score,
                "top_reference_score": top_reference_score,
                "quality_score": embedding.quality_score,
                "minimum_quality": self._minimum_quality,
                "policy_version": str(policy["policy_version"]),
                "self_threshold": self_threshold,
                "not_self_threshold": not_self_threshold,
                "reference_count": int(references.shape[0]),
            },
        )

    def _load(
        self,
    ) -> tuple[tuple[dict[str, Any], np.ndarray, np.ndarray] | None, str]:
        if not self._policy_path.is_file():
            return None, "active_self_identity_policy_missing"
        try:
            policy = json.loads(self._policy_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, "active_self_identity_policy_invalid"
        if policy.get("accepted") is not True or policy.get("blockers"):
            return None, "active_self_identity_policy_not_accepted"
        required = {
            "policy_version",
            "self_threshold",
            "not_self_threshold",
            "false_accept_rate",
            "false_reject_rate",
            "voiceprint",
            "voiceprint_sha256",
        }
        if not required.issubset(policy):
            return None, "active_self_identity_policy_incomplete"
        voiceprint = Path(str(policy["voiceprint"])).resolve()
        allowed_roots = (
            (self._state_dir / "identity").resolve(),
            (self._state_dir.parent / "voiceprints").resolve(),
        )
        if not any(voiceprint.is_relative_to(root) for root in allowed_roots):
            return None, "voiceprint_path_outside_identity_roots"
        if not voiceprint.is_file():
            return None, "voiceprint_missing"
        if _sha256(voiceprint) != str(policy["voiceprint_sha256"]):
            return None, "voiceprint_digest_mismatch"
        try:
            with np.load(voiceprint, allow_pickle=False) as payload:
                references = np.asarray(payload["embeddings"], dtype=np.float32)
                centroid = np.asarray(payload["centroid"], dtype=np.float32)
        except (OSError, ValueError, KeyError):
            return None, "voiceprint_invalid"
        if (
            references.ndim != 2
            or not references.shape[0]
            or centroid.shape != (references.shape[1],)
        ):
            return None, "voiceprint_dimensions_invalid"
        reference_norms = np.linalg.norm(references, axis=1, keepdims=True)
        centroid_norm = float(np.linalg.norm(centroid))
        if np.any(reference_norms == 0) or centroid_norm == 0:
            return None, "voiceprint_contains_zero_norm_vector"
        references = references / reference_norms
        centroid = centroid / centroid_norm
        return (policy, references, centroid), "accepted_calibrated_voiceprint"


def _unknown(reason: str) -> IdentityDecision:
    return IdentityDecision(
        SelfIdentity.UNKNOWN,
        {
            "source": "calibrated_self_voiceprint",
            "decision": SelfIdentity.UNKNOWN.value,
            "reason": reason,
        },
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


__all__ = ["CalibratedSelfIdentityMatcher"]
