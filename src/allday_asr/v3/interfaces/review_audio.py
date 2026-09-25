"""Bounded, gap-free audition of exactly the stored representative windows."""
import hashlib
import io
import json
import wave


def audio_plan(candidate):
    clips = candidate.get('representative_clips') or []
    if not 1 <= len(clips) <= 5:
        raise ValueError('sample must contain one to five audio windows')
    windows = []
    total = 0
    for clip in clips:
        media, start, end = clip.get('media_id'), clip.get('start_ms'), clip.get('end_ms')
        if (not isinstance(media, str) or not media or type(start) is not int
                or type(end) is not int or start < 0 or end <= start):
            raise ValueError('invalid sample audio window')
        if any(w['media_id'] == media and start < w['end_ms'] and end > w['start_ms'] for w in windows):
            raise ValueError('overlapping or repeated sample audio windows')
        windows.append(dict(media_id=media, start_ms=start, end_ms=end, playback_start_ms=total))
        total += end - start
    if total > 40_000:
        raise ValueError('sample audio exceeds 40 seconds')
    key = hashlib.sha256(json.dumps([candidate['prototype_id'], windows], sort_keys=True).encode()).hexdigest()
    return dict(audition_key=key, windows=windows, total_ms=total, window_count=len(windows))


def concatenate(parts):
    if len(parts) == 1:
        return parts[0]
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        expected = None
        for part in parts:
            with wave.open(io.BytesIO(part), 'rb') as source:
                signature = (source.getnchannels(), source.getsampwidth(), source.getframerate())
                if expected is None:
                    expected = signature
                    target.setnchannels(signature[0])
                    target.setsampwidth(signature[1])
                    target.setframerate(signature[2])
                elif expected != signature:
                    raise ValueError('incompatible normalized review audio')
                target.writeframes(source.readframes(source.getnframes()))
    return output.getvalue()
