from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest

from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError
from allday_asr.v3.interfaces.transfer.passkeys import RequestBinding


class _FakeDesktop:
    def __init__(self, people) -> None:
        self.people = people

    def list_reviews(self, _limit: int):
        prototype_ids = [value["prototype_id"] for value in self.people.candidates]
        if not prototype_ids:
            return ()
        return (
            {
                "review_id": "voice_identity:cluster:person:primary",
                "kind": "voice_identity",
                "priority": "normal",
                "source_id": "cluster",
                "source_revision": None,
                "session_id": None,
                "person_id": "person",
                "title": "张同学",
                "summary": "声音共同指向张同学",
                "reason": "voice_identity_requires_confirmation",
                "evidence_count": len(prototype_ids),
                "created_at": "2026-09-02T00:00:00Z",
                "updated_at": "2026-09-02T00:00:00Z",
                "context": {
                    "voice_mode": "known_person",
                    "prototype_ids": prototype_ids,
                },
            },
        )

    def media(self, media_id: str):
        assert media_id == "media-1"
        return {
            "storage_key": "audio-key",
            "format": "wav",
            "size_bytes": 8,
        }


class _FakePeople:
    def __init__(self) -> None:
        self.candidates = [
            {
                "prototype_id": "strong",
                "session_id": "session-2",
                "speaker_track_id": "track-2",
                "person_id": "person",
                "person_name": "张同学",
                "quality_score": 0.95,
                "best_score": 0.92,
                "score_margin": 0.21,
                "review_status": "pending",
                "representative_clips": [
                    {"media_id": "media-1", "start_ms": 2000, "end_ms": 5000}
                ],
            },
            {
                "prototype_id": "weak",
                "session_id": "session-1",
                "speaker_track_id": "track-1",
                "person_id": "person",
                "person_name": "张同学",
                "quality_score": 0.82,
                "best_score": 0.72,
                "score_margin": 0.07,
                "review_status": "pending",
                "representative_clips": [
                    {"media_id": "media-1", "start_ms": 1000, "end_ms": 3000}
                ],
            },
        ]
        self.reviews = []

    def list_review_candidates(self, _person_id, _status, _limit):
        return tuple(value for value in self.candidates if _status is None or value["review_status"] == _status)

    def review_prototype(self, prototype_id, person_id, decision, **values):
        self.reviews.append((prototype_id, person_id, decision, values["actor"]))
        self.candidates = [
            value for value in self.candidates if value["prototype_id"] != prototype_id
        ]
        return {"prototype_id": prototype_id, "decision": decision}


class _FakeAudioStore:
    root = Path("state/v3/audio")

    def path_for(self, key: str):
        assert key == "audio-key"
        return Path("source.wav")


def _service():
    people = _FakePeople()
    core = SimpleNamespace(
        people=people,
        desktop=_FakeDesktop(people),
        audio_store=_FakeAudioStore(),
    )
    return DeviceReviewService(core), people


def test_review_snapshot_enriches_and_orders_voice_samples_without_vectors() -> None:
    service, _people = _service()

    item = service.snapshot()["items"][0]
    candidates = item["context"]["voice_candidates"]

    assert [value["prototype_id"] for value in candidates] == ["weak", "strong"]
    assert candidates[0]["representative_clips"][0]["start_ms"] == 1000
    assert "vector" not in candidates[0]


def test_voice_review_resolves_only_one_current_sample_and_returns_fresh_snapshot() -> None:
    service, people = _service()

    result = service.resolve(
        "phone-1",
        {
            "review_id": "voice_identity:cluster:person:primary",
            "action": "confirm",
            "prototype_id": "weak",
        },
    )

    assert people.reviews == [("weak", "person", "confirmed", "phone-device:phone-1")]
    remaining = result["reviews"]["items"][0]["context"]["voice_candidates"]
    assert [value["prototype_id"] for value in remaining] == ["strong"]
    with pytest.raises(DeviceConflictError):
        service.resolve(
            "phone-1",
            {
                "review_id": "voice_identity:cluster:person:primary",
                "action": "reject",
                "prototype_id": "weak",
            },
        )


def test_review_audio_is_bound_to_a_current_sample_and_loudness_normalized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path('source.wav').write_bytes(b'synthetic source; renderer substituted')
    rendered = {}

    def fake_normalized_audio(source, start_ms, end_ms, *, temp_root):
        rendered.update(
            source=source,
            start_ms=start_ms,
            end_ms=end_ms,
            temp_root=temp_root,
        )
        return b"RIFFnormalized"

    monkeypatch.setattr(
        "allday_asr.v3.interfaces.device_reviews._normalized_review_audio",
        fake_normalized_audio,
    )
    service, _people = _service()

    response = service.audio(
        {
            "review_id": "voice_identity:cluster:person:primary",
            "prototype_id": "weak",
        }
    )

    assert (
        base64.urlsafe_b64decode(response["data_base64url"] + "==")
        == b"RIFFnormalized"
    )
    assert response["format"] == "wav"
    assert (response["start_ms"], response["end_ms"]) == (0, 2000)
    assert rendered == {
        "source": Path("source.wav"),
        "start_ms": 1000,
        "end_ms": 3000,
        "temp_root": Path("state/v3/review-audio-temp"),
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/device/v3/reviews"),
        ("POST", "/device/v3/reviews/action"),
        ("POST", "/device/v3/reviews/audio"),
        ("POST", "/device/v3/annotations"),
    ],
)
def test_device_auth_binding_allows_only_the_review_contract(method: str, path: str) -> None:
    RequestBinding.for_request(method=method, path=path, body=b"{}" if method == "POST" else b"")


def test_discovery_exposes_and_plays_cluster_samples(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path('source.wav').write_bytes(b'synthetic source; renderer substituted')
    service, people = _service()
    original = service.core.desktop.list_reviews(500)[0]
    discovery = {**original, "source_id": "cluster", "context": {"voice_mode": "speaker_discovery"}}
    monkeypatch.setattr(service.core.desktop, "list_reviews", lambda limit: [discovery])
    people.cluster = lambda cluster_id: {
        "members": [{"speaker_track_id": "track-1", "session_id": "session-1"}],
        "prototypes": [{**people.candidates[1], "status": "candidate"}],
    }
    monkeypatch.setattr("allday_asr.v3.interfaces.device_reviews._normalized_review_audio",
                        lambda *args, **kwargs: b"RIFFsample")
    item = service.snapshot()["items"][0]
    assert item["context"]["voice_candidates"][0]["session_id"] == "session-1"
    response = service.audio({"review_id": item["review_id"], "prototype_id": "weak"})
    assert response["end_ms"] == 2000
    with pytest.raises(DeviceConflictError):
        service.audio({"review_id": item["review_id"], "prototype_id": "strong"})
