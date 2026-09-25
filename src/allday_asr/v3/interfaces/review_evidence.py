"""Map immutable media windows to current sentences without a track-wide fallback."""
import hashlib
import json


def candidate_evidence(candidate, detail):
    result = []
    session_id = candidate['session_id']
    for index, clip in enumerate(candidate.get('representative_clips', [])):
        media, start, end = (clip.get(k) for k in ('media_id', 'start_ms', 'end_ms'))
        if not isinstance(media, str) or type(start) is not int or type(end) is not int or not 0 <= start < end:
            return []
        segments = [s for s in detail.get('segments', []) if s['media_id'] == media
                    and s['source_start_ms'] <= start and end <= s['source_end_ms']
                    and end - s['source_start_ms'] <= s['session_end_ms'] - s['session_start_ms']]
        # Reused media or a window crossing captures is ambiguous: no guessed mapping.
        if len(segments) != 1:
            return []
        segment = segments[0]
        offset = segment['session_start_ms'] - segment['source_start_ms']
        for row in detail.get('utterances', []):
            if (row['session_id'] != session_id or row['status'] != 'active'
                    or row['start_ms'] >= end + offset or row['end_ms'] <= start + offset):
                continue
            result.append(dict(utterance_id=row['utterance_id'], revision=row['revision'],
                session_id=session_id, utterance_start_ms=row['start_ms'], utterance_end_ms=row['end_ms'],
                window_index=index, media_id=media, clip_start_ms=start, clip_end_ms=end,
                session_start_ms=start + offset, session_end_ms=end + offset))
    return result


def candidate_key(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
