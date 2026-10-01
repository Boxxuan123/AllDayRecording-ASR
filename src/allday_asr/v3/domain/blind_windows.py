"""Deterministic single-media CAM++ windows; no identity thresholds or truth."""

from .speaker_turns import QUERY_BUILDER_VERSION, foreign_turns, safe_ranges

MIN_WINDOW_MS = 800
MAX_WINDOW_MS = 8000
BOUNDARY_SEARCH_MS = 1200
MAX_QUERY_WINDOWS = 5


def _natural_end(start, limit, span_end, turns, speaker, tokens, vad):
    if limit == span_end:
        return limit, "speaker_turn" if any(
            t["speaker_label"] == speaker and t["end_ms"] == limit for t in turns
        ) else "utterance"
    low = max(start + MIN_WINDOW_MS, limit - BOUNDARY_SEARCH_MS)
    candidates = [
        ("speaker_turn", [t["end_ms"] for t in turns if t["speaker_label"] == speaker]),
        ("token", [t["end_ms"] for t in tokens]),
        ("vad", [v[1] for v in vad]),
    ]
    for reason, boundaries in candidates:
        valid = [b for b in boundaries if low <= b <= limit
                 and not any(t["start_ms"] < b < t["end_ms"] for t in tokens)]
        if valid:
            return max(valid), reason
    return limit, "hard_cap"


def plan_source_windows(utterance, speaker, track_id, exclusive_turns, tokens,
                        vad_ranges, captures, projection_version):
    """Return every eligible continuation plus explicit exclusions.

    Caller chooses a bounded query subset separately. Storage adjacency is
    necessary for continuation; no tolerance silently fills missing audio.
    """
    start, end = utterance["start_ms"], utterance["end_ms"]
    ranges = safe_ranges(start, end, speaker, exclusive_turns)
    windows, exclusions = [], []
    for turn in foreign_turns(start, end, speaker, exclusive_turns):
        exclusions.append({"start_ms": max(start, turn["start_ms"]),
                           "end_ms": min(end, turn["end_ms"]),
                           "reason": "foreign_exclusive_turn"})
    captures = sorted(captures, key=lambda c: (c["session_start_ms"], c["session_end_ms"],
                                              c["media_id"]))
    if len({c["session_id"] for c in captures}) > 1:
        raise ValueError("cannot continue across recording sessions")
    for safe_start, safe_end in ranges:
        cursor = safe_start
        first = True
        previous_capture = None
        for index, capture in enumerate(captures):
            cap_start, cap_end = capture["session_start_ms"], capture["session_end_ms"]
            if cap_end <= cursor or cap_start >= safe_end:
                continue
            if cap_start > cursor:
                exclusions.append({"start_ms": cursor, "end_ms": safe_end,
                                   "reason": "capture_gap_no_continuation"})
                break
            if previous_capture and cap_start != previous_capture["session_end_ms"]:
                exclusions.append({"start_ms": cursor, "end_ms": safe_end,
                                   "reason": "capture_mapping_not_contiguous"})
                break
            if (capture["source_end_ms"] - capture["source_start_ms"] != cap_end - cap_start
                    or capture["source_start_ms"] < 0
                    or capture["source_end_ms"] > capture["duration_ms"]):
                exclusions.append({"start_ms": cursor, "end_ms": safe_end,
                                   "reason": "invalid_capture_mapping"})
                break
            piece_end = min(safe_end, cap_end)
            next_capture = captures[index + 1] if index + 1 < len(captures) else None
            can_continue = bool(piece_end < safe_end and next_capture
                                and next_capture["session_start_ms"] == cap_end
                                and next_capture["session_id"] == capture["session_id"]
                                and next_capture["source_end_ms"] - next_capture["source_start_ms"]
                                    == next_capture["session_end_ms"] - next_capture["session_start_ms"]
                                and 0 <= next_capture["source_start_ms"] < next_capture["source_end_ms"]
                                    <= next_capture["duration_ms"])
            boundary_start = ("speaker_turn" if safe_start != start else "utterance") if first else "capture_edge_continue"
            while cursor < piece_end:
                limit = min(cursor + MAX_WINDOW_MS, piece_end)
                stop, reason = _natural_end(cursor, limit, safe_end, exclusive_turns,
                                            speaker, tokens, vad_ranges)
                if stop == cap_end and stop < safe_end:
                    reason = "capture_edge_continue" if can_continue else "media_end"
                if stop - cursor < MIN_WINDOW_MS:
                    exclusions.append({"start_ms": cursor, "end_ms": stop,
                                       "reason": "below_minimum_useful_duration"})
                else:
                    source = capture["source_start_ms"] + cursor - cap_start
                    coverage = [dict(t) for t in exclusive_turns
                                if t["start_ms"] < stop and t["end_ms"] > cursor]
                    if foreign_turns(cursor, stop, speaker, exclusive_turns):
                        raise ValueError("source window crosses foreign exclusive turn")
                    provenance = {
                        "query_builder_version": QUERY_BUILDER_VERSION,
                        "projection_version": projection_version,
                        "utterance_id": utterance["utterance_id"],
                        "original_utterance_range": [start, end],
                        "speaker_label": speaker, "speaker_track_id": track_id,
                        "exclusive_turn_coverage": coverage,
                        "start_boundary_reason": boundary_start, "end_boundary_reason": reason,
                        "capture_continuation": previous_capture is not None or can_continue,
                    }
                    windows.append({"media_id": capture["media_id"], "sha256": capture["sha256"],
                                    "storage_key": capture["storage_key"], "start_ms": source,
                                    "end_ms": source + stop - cursor, "session_start_ms": cursor,
                                    "session_end_ms": stop, "utterance_id": utterance["utterance_id"],
                                    "provenance": provenance})
                cursor = stop
                boundary_start = reason
                first = False
            previous_capture = capture
            if cursor == safe_end:
                break
            if not can_continue:
                exclusions.append({"start_ms": cursor, "end_ms": safe_end,
                                   "reason": "capture_edge_without_valid_continuation"})
                break
        if cursor < safe_end and not any(e["start_ms"] <= cursor and e["end_ms"] >= safe_end
                                         for e in exclusions):
            exclusions.append({"start_ms": cursor, "end_ms": safe_end,
                               "reason": "no_available_capture"})
    return {"windows": windows, "exclusions": exclusions}
