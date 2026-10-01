"""Exclusive turns are temporal boundaries; tracks are identity groupings."""

from collections.abc import Mapping, Sequence

PROJECTION_VERSION = "exclusive-turn-preserving-v2"
QUERY_BUILDER_VERSION = "turn-safe-boundary-aware-v2"
LEGACY_PROJECTION_VERSION = "native-token-grouping-v1"
LEGACY_QUERY_BUILDER_VERSION = "longest5-head8s-v1"


def foreign_turns(start, end, speaker, turns):
    """Regular overlap output must not be supplied as exclusive boundaries."""
    if speaker is None:
        return []
    return [dict(t) for t in turns if t["speaker_label"] != speaker
            and t["start_ms"] < end and t["end_ms"] > start]


def safe_ranges(start, end, speaker, exclusive_turns):
    """Subtract detected foreign exclusive speech, including token edge crossings."""
    cursor, result = start, []
    for turn in sorted(foreign_turns(start, end, speaker, exclusive_turns),
                       key=lambda t: (t["start_ms"], t["end_ms"])):
        lo, hi = max(start, turn["start_ms"]), min(end, turn["end_ms"])
        if cursor < lo:
            result.append((cursor, lo))
        cursor = max(cursor, hi)
    if cursor < end:
        result.append((cursor, end))
    return result


def can_merge_same_speaker_tokens(previous: Mapping, following: Mapping,
                                  exclusive_turns: Sequence) -> bool:
    return (previous["speaker"] == following.get("speaker")
            and following["start_ms"] - previous["end_ms"] <= 1200
            and following["end_ms"] - previous["start_ms"] <= 30000
            and not foreign_turns(previous["start_ms"], following["end_ms"],
                                  previous["speaker"], exclusive_turns)
            and not previous.get("speaker_boundary_crossing", False)
            and not following.get("speaker_boundary_crossing", False))
