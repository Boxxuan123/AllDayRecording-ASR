"""Public evidence identity projection; review uncertainty overrides identity."""
from typing import Any


def project_evidence_identity(
    value: dict[str, Any],
    speaker_reference: dict[str, Any] | None,
) -> dict[str, Any]:
    if "person" in value.get("evidence", {}).get("annotation_review", {}).get("dimensions", {}):
        speaker_reference = None
        value = {**value, "identity": "unknown", "speaker_label": None, "speaker_track_id": None}
    speaker_reference = speaker_reference or {}
    annotation = value.get("evidence", {}).get("person_annotation", {})
    if annotation.get("person_id") and "person" not in value.get("evidence", {}).get("annotation_review", {}).get("dimensions", {}):
        person_id = annotation["person_id"]
        speaker_reference = {**speaker_reference, "person_id": person_id,
            "person_name": speaker_reference.get("person_name") if speaker_reference.get("person_id") == person_id else None}
    return {
        "utterance_id": str(value["utterance_id"]),
        "start_at": str(value["start_at"]),
        "end_at": str(value["end_at"]),
        "speaker_label": value.get("speaker_label"),
        "speaker_person_id": speaker_reference.get("person_id"),
        "speaker_name": speaker_reference.get("person_name"),
        "identity": str(value.get("identity") or "unknown"),
        "text": str(value["text"]),
        "revision": int(value["revision"]),
    }

