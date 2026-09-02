from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.people import PersonIdentityMaturity, PersonIdentityPolicy, PersonKind

_CLUSTER_IDENTITY_ACTOR_PREFIX = "system:speaker-cluster-identity:"
_KNOWN_PERSON_POLICY_VERSION = "known-person-layered-v1"


def _compatible(
    vector: tuple[float, ...], candidates: tuple[tuple[str, tuple[float, ...]], ...]
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    return tuple(candidate for candidate in candidates if len(candidate[1]) == len(vector))


def _identity_policies(
    values: dict[str, dict[str, Any]],
) -> dict[str, PersonIdentityPolicy]:
    return {
        person_id: PersonIdentityPolicy(
            person_id=person_id,
            revision=int(value["revision"]),
            maturity_status=PersonIdentityMaturity(str(value["maturity_status"])),
            auto_match_enabled=bool(value["auto_match_enabled"]),
            suggest_threshold=float(value["suggest_threshold"]),
            auto_accept_threshold=float(value["auto_accept_threshold"]),
            minimum_margin=float(value["minimum_margin"]),
            minimum_quality=float(value["minimum_quality"]),
        )
        for person_id, value in values.items()
    }


def _identity_confidence(evidence: dict[str, Any]) -> float:
    raw = evidence.get("score")
    if not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(raw)))


def _replace_reference(value: Any, old: str, new: str) -> Any:
    if value == old:
        return new
    if isinstance(value, dict):
        return {key: _replace_reference(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_reference(item, old, new) for item in value]
    return value


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("speaker identity timestamps require timezone information")
    return value.isoformat()


def _identity_for_person_kind(kind: str) -> SelfIdentity:
    if kind == PersonKind.SELF.value:
        return SelfIdentity.SELF
    if kind == PersonKind.KNOWN.value:
        return SelfIdentity.NOT_SELF
    raise ValueError(f"person kind cannot resolve utterance identity: {kind}")
