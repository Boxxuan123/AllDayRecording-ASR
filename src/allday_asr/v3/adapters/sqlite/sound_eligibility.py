"""SQL equivalent of the shared content-purpose rule; aliases are internal."""
from allday_asr.v3.domain.sound_kind import CONTENT_SOUND_KINDS, SAMPLE_SOUND_KINDS


def usable_content(alias: str) -> str:
    evidence = f"{alias}.evidence_json"
    kinds = ", ".join(f"'{kind}'" for kind in sorted(CONTENT_SOUND_KINDS))
    return f"""(COALESCE(json_array_length({evidence}, '$.annotation_outdated'), 0) = 0 AND json_type({evidence}, '$.annotation_review.dimensions.sound') IS NULL AND COALESCE(json_extract({evidence}, '$.content_reliable'),
        COALESCE(json_extract({evidence}, '$.sound_kind'), 'speech') IN ({kinds})) = 1
        AND COALESCE(json_extract({evidence}, '$.sound_source'), 'unknown') != 'media'
        AND COALESCE(json_extract({evidence}, '$.sound_kind'), 'speech') != 'media_speech'
        AND COALESCE(json_extract({evidence}, '$.actual_interaction'), 1) != 0)"""


def confirmed_interaction(alias: str) -> str:
    return f"""({usable_content(alias)}
        AND json_type({alias}.evidence_json, '$.annotation_review.dimensions.person') IS NULL AND COALESCE(
        json_extract({alias}.evidence_json, '$.actual_interaction'),
        json_extract({alias}.evidence_json, '$.sound_kind') IN ('remote_speech', 'live_speech'), 0) = 1)"""


def usable_sample(alias: str) -> str:
    evidence = f"{alias}.evidence_json"
    kinds = ", ".join(f"'{kind}'" for kind in sorted(SAMPLE_SOUND_KINDS))
    return f"""(COALESCE(json_array_length({evidence}, '$.annotation_outdated'), 0) = 0 AND json_type({evidence}, '$.annotation_review.dimensions.sound') IS NULL AND COALESCE(json_extract({evidence}, '$.sound_kind'), 'speech') IN ({kinds})
        AND json_type({evidence}, '$.annotation_review.dimensions.person') IS NULL
        AND COALESCE(json_extract({evidence}, '$.has_overlap'), 0) != 1
        AND COALESCE(json_extract({evidence}, '$.sound_source'), 'unknown') != 'media'
        AND COALESCE(json_extract({evidence}, '$.actual_interaction'), 1) != 0
        AND COALESCE(json_extract({evidence}, '$.voice_sample_eligible'), 1) != 0)"""
