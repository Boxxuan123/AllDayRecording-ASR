from __future__ import annotations

from http import HTTPStatus
from pathlib import Path


class AudioRangeResponseMixin:
    """Stream a local WAV file with a single RFC 7233 byte range."""

    def _send_audio_range(self, path: Path) -> None:
        size = path.stat().st_size
        start = 0
        end = size - 1
        status = HTTPStatus.OK
        range_header = self.headers.get("Range")
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise ValueError("只支持单个音频 byte range")
            raw_start, _, raw_end = range_header[6:].partition("-")
            if not raw_start:
                suffix_length = int(raw_end)
                start = max(0, size - suffix_length)
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end else end
            if start < 0 or end < start or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self._security_headers()
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "private, max-age=3600")
        self._security_headers()
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
