from __future__ import annotations

from urllib.parse import parse_qs

from allday_asr.interfaces.web.params import match_path


def get_audio(handler, parsed) -> bool:
    match = match_path(parsed.path, r"/api/audio/(?P<segment_id>\d+)")
    if not match:
        return False
    mode = parse_qs(parsed.query).get("mode", ["segment"])[0]
    handler._send_file(
        handler.application.audio_clip(int(match["segment_id"]), mode=mode),
        "audio/wav",
    )
    return True
