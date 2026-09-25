"""Human segment labels; absence means ordinary speech for older projections."""
SAMPLE_SOUND_KINDS = frozenset({"speech", "live_speech"})
CONTENT_SOUND_KINDS = SAMPLE_SOUND_KINDS | {"overlapping_speech", "remote_speech"}
SOUND_KINDS = CONTENT_SOUND_KINDS | {"non_speech", "unintelligible", "background_speech", "media_speech"}


def is_usable_speech(evidence: dict) -> bool:
    return sound_uses(evidence)["content_usable"]


def sound_uses(evidence: dict) -> dict:
    """Independent purposes; legacy background remains an unknown source.

    Identity is deliberately absent: even an unusable sample can identify a
    person. Remote participation alone is not a content exclusion.
    """
    pending = evidence.get("annotation_review", {}).get("dimensions", {})
    if "sound" in pending or evidence.get("annotation_outdated"):
        return {"content_usable": False, "source": "unknown", "actual_interaction": False,
                "sample_candidate_allowed": False, "sample_exclusion_reason": "ambiguous_audio_mapping"}
    kind = evidence.get("sound_kind", "speech")
    source = evidence.get("sound_source", {"live_speech": "live", "remote_speech": "remote", "media_speech": "media"}.get(kind, "unknown"))
    interaction = evidence.get("actual_interaction", {"live_speech": True, "remote_speech": True, "media_speech": False}.get(kind))
    reliable = evidence.get("content_reliable", kind in CONTENT_SOUND_KINDS or kind == "media_speech") is True
    sample_reason = None
    if kind not in SAMPLE_SOUND_KINDS:
        sample_reason = kind
    elif evidence.get("has_overlap") is True:
        sample_reason = "overlapping_speakers"
    elif source == "media" or interaction is False:
        sample_reason = "not_actual_interaction"
    elif evidence.get("voice_sample_eligible") is False:
        sample_reason = "human_excluded_sample"
    if "person" in pending:
        sample_reason = "ambiguous_audio_mapping"
    return {"content_usable": reliable and source != "media" and interaction is not False,
            "source": source, "actual_interaction": interaction,
            "sample_candidate_allowed": sample_reason is None,
            "sample_exclusion_reason": sample_reason}
