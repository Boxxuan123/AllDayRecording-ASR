from __future__ import annotations

import base64
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from allday_asr.v3.interfaces.device_annotations import DeviceAnnotationService
from allday_asr.v3.interfaces.transfer.http_review_routes import dispatch_review_post


def test_audio_assembles_session_offsets_across_files_and_cleans_up(monkeypatch):
    root = Path(__file__).parent / f"annotation-audio-{uuid4().hex}"
    seen = []

    def descriptors(session_id, start, end):
        assert (session_id, start, end) == ("session", 9000, 12000)
        return [
            {"storage_key": "first", "start_ms": 9000, "end_ms": 10000},
            {"storage_key": "second", "start_ms": 0, "end_ms": 2000},
        ]

    def assemble(clips, output):
        seen.extend(clips)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"RIFFjoined")

    core = SimpleNamespace(
        desktop=SimpleNamespace(session_audio_clips=descriptors),
        audio_store=SimpleNamespace(
            root=root / "audio", path_for=lambda key: root / key
        ),
    )
    monkeypatch.setattr(
        "allday_asr.v3.interfaces.device_annotations.assemble_audio_clips", assemble
    )
    try:
        result = DeviceAnnotationService(core).execute(
            "phone",
            {
                "action": "audio",
                "session_id": "session",
                "start_ms": 9000,
                "end_ms": 12000,
            },
        )
        assert [(c.start_ms, c.end_ms) for c in seen] == [(9000, 10000), (0, 2000)]
        assert result["audio"]["start_ms"] == 0
        assert result["audio"]["end_ms"] == 3000
        assert (
            base64.urlsafe_b64decode(result["audio"]["data_base64url"] + "==")
            == b"RIFFjoined"
        )
        assert not list(root.rglob("*.wav"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    "start,end", [(True, 1000), (-1, 100), (3, 3), (0, 300001), ("0", 5)]
)
def test_audio_rejects_invalid_or_unbounded_ranges(start, end):
    with pytest.raises(ValueError):
        DeviceAnnotationService(SimpleNamespace()).execute(
            "phone",
            {
                "action": "audio",
                "session_id": "session",
                "start_ms": start,
                "end_ms": end,
            },
        )


def test_annotation_route_authenticates_exact_body_before_dispatch():
    calls = []
    raw = b'{"action":"people"}'

    class Handler:
        server = SimpleNamespace(
            v3_gateway=SimpleNamespace(
                annotations=lambda device, payload: (
                    calls.append((device, payload)) or {"people": []}
                )
            )
        )

        def _read_body(self, *, max_bytes):
            return raw

        def _authenticate_device_request(self, binding):
            calls.append((binding.method, binding.path))
            return SimpleNamespace(device_id="paired-phone")

        def _decode_json(self, body):
            assert body == raw
            return {"action": "people"}

        def _send_json(self, status, response):
            assert status == 200
            assert response == {"people": []}

    assert dispatch_review_post(Handler(), "/device/v3/annotations")
    assert calls == [
        ("POST", "/device/v3/annotations"),
        ("paired-phone", {"action": "people"}),
    ]
