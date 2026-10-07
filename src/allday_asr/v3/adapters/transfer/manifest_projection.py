"""Phone manifest and audio asset payloads published to the device stream."""

from collections.abc import Mapping
from typing import Any

from allday_asr.v3.domain.models import AudioAsset, RecordingSession


def session_projection(
    session: RecordingSession, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        # Phone owns the original audio files under this stable manifest key.
        "session_key": manifest["sessionKey"],
        "captured_start": session.captured_start.isoformat().replace("+00:00", "Z"),
        "captured_end": (
            session.captured_end.isoformat().replace("+00:00", "Z")
            if session.captured_end is not None else None
        ),
        "timezone": session.timezone,
        "state": session.state.value,
        "revision": session.revision,
        "status_code": session.status_code,
        "progress": session.progress,
        "device_name": manifest["device"],
    }


def asset_projection(asset: AudioAsset, session_id: str, sequence: int) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "session_id": session_id,
        "sequence": sequence,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "duration_ms": asset.duration_ms,
        "format": asset.format.value,
    }
