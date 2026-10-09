"""A source snapshot is validated within the existing publishing transaction."""
from allday_asr.v3.domain.hashing import canonical_json_sha256


def validate_generation_snapshot(uow, scope):
    snapshot = scope.get("input_snapshot")
    if not snapshot or snapshot.get("kind") != "event-request":
        return
    from dataclasses import asdict
    from .event_extraction import semantic_request_from_detail
    expected = snapshot["inputs"]
    current = asdict(semantic_request_from_detail(expected["session_id"],
        uow.desktop.session_detail(expected["session_id"])))
    if canonical_json_sha256(expected) != canonical_json_sha256(current):
        raise ValueError("event generation input snapshot changed")
