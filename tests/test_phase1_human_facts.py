"""Equivalent R1/R2/R3/R9 reproductions; original independent audit unavailable."""

from dataclasses import replace
from uuid import uuid4
from unittest.mock import patch
import pytest
from tests import test_v33_intelligent_reminders as reminder_fixture
from tests.test_v33_intelligent_reminders import _intent, UTTERANCE_ID
from tests import test_v34_open_speaker_identity as people_fixture
from tests.test_v34_open_speaker_identity import _utterance_id
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.application import CorrectionInvalidationService


@pytest.fixture
def reminders():
    f = reminder_fixture.V33IntelligentReminderTests()
    f.setUp()
    try:
        yield f
    finally:
        f.tearDown()


@pytest.fixture
def people():
    f = people_fixture.V34OpenSpeakerIdentityTests()
    f.setUp()
    f._seed_track(1)
    original = f.provider.embed

    def embed(tracks):
        for t in tracks:
            f.provider.vectors[t.speaker_track_id] = (1.0, 0.0, 0.0)
        return original(tracks)

    with patch.object(f.provider, "embed", side_effect=embed):
        try:
            yield f
        finally:
            f.tearDown()


def assign(f, uid, name):
    with SqliteUnitOfWork(f.core.database) as uow:
        revision = uow.evidence.get_utterance(uid).revision
    result = f.core.people.assign_utterances(
        [{"utterance_id": uid, "revision": revision}],
        person_id=None,
        display_name=name,
        actor="human",
    )
    f.core.people.sample_worker.run_pending()
    return result


def rerun(f, **changes):
    with SqliteUnitOfWork(f.core.database) as uow:
        old = uow.evidence.get_utterance(_utterance_id(1))
        uid = stable_ulid(str(uuid4()))
        uow.evidence.add_utterance(
            replace(
                old,
                utterance_id=uid,
                ordinal=int(uuid4().int % 100000000),
                evidence={},
                revision=1,
                **changes,
            )
        )
        return uow.evidence.get_utterance(uid)


def test_confirmed_reminder_survives_nonsemantic_sound_label_change(reminders):
    f = reminders
    before = f._confirmed(_intent())["reminder"]
    CorrectionInvalidationService(
        f.factory, now=lambda: f.current_time
    ).classify_segments([{"utterance_id": UTTERANCE_ID, "revision": 1}], "live_speech")
    after = f.reminders.schedule(before["event_id"])
    for field in ("status", "title", "scheduled_at", "event_revision"):
        assert after[field] == before[field]
    with f.factory() as uow:
        changes = [
            c for c in uow.changes.list_after(0, 500) if c.resource_type == "reminder"
        ]
        assert changes[-1].payload["status"] == "scheduled"


@pytest.mark.parametrize("kind", ["non_speech", "media_speech"])
def test_rerun_preserves_sound_exclusion_without_person(people, kind):
    f = people
    f.core.corrections.classify_segments(
        [{"utterance_id": _utterance_id(1), "revision": 1}], kind
    )
    restored = rerun(f)
    assert restored.evidence.get("sound_kind") == kind
    assert not sound_uses(restored.evidence)["content_usable"]


def test_reassigning_restored_audio_invalidates_old_person_sample(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    c = f.core.people.list_review_candidates(a["person_id"])[0]
    f.core.people.review_prototype(c["prototype_id"], a["person_id"], "confirmed")
    f._seed_reprocessed_track(1)
    with SqliteUnitOfWork(f.core.database) as uow:
        template = uow.evidence.get_utterance(_utterance_id(1, reprocessed=True))
        uid = stable_ulid("phase1-restored")
        uow.evidence.add_utterance(replace(template, utterance_id=uid, ordinal=99))
        assert (
            uow.evidence.get_utterance(uid).evidence["person_annotation"]["person_id"]
            == a["person_id"]
        )
        assert uow.people.person_vectors(f.provider.model, f.provider.model_version)
    assign(f, uid, "B")
    with SqliteUnitOfWork(f.core.database) as uow:
        assert not uow.people.person_vectors(f.provider.model, f.provider.model_version)


def test_unambiguous_latest_correction_wins_on_next_rerun(people):
    f = people
    assign(f, _utterance_id(1), "A")
    f._seed_reprocessed_track(1)

    def restore():
        with SqliteUnitOfWork(f.core.database) as uow:
            template = uow.evidence.get_utterance(_utterance_id(1, reprocessed=True))
            uid = stable_ulid(str(uuid4()))
            uow.evidence.add_utterance(
                replace(
                    template, utterance_id=uid, ordinal=int(uuid4().int % 100000000)
                )
            )
            return uow.evidence.get_utterance(uid)

    current = restore()
    b = assign(f, current.utterance_id, "B")
    final = restore()
    assert (
        final.evidence.get("person_annotation", {}).get("person_id") == b["person_id"]
    )
    assert "annotation_review" not in final.evidence


def test_phone_refresh_uses_real_scheduler(reminders):
    import json
    import subprocess
    from pathlib import Path
    from allday_asr.v3.application.mobile_sync import MobileSyncService
    from allday_asr.v3.domain.device_sync import SyncRequest, PROJECTION_VERSION

    f = reminders
    result = f._confirmed(_intent())["reminder"]
    sync = MobileSyncService(f.factory, now=lambda: f.current_time)
    first = sync.synchronize(
        "computer-1", SyncRequest(PROJECTION_VERSION, None, (), 500)
    )
    CorrectionInvalidationService(
        f.factory, now=lambda: f.current_time
    ).classify_segments([{"utterance_id": UTTERANCE_ID, "revision": 1}], "live_speech")
    second = sync.synchronize(
        "computer-1", SyncRequest(PROJECTION_VERSION, first.next_cursor, (), 500)
    )
    import os
    phone = Path(os.environ.get("ALLDAY_HARMONY_REPO", "C:/Users/32673/DevEcoStudioProjects/AllDayRecording"))
    command = [
        "node",
        str(phone / "tools/quality/check-phase1-reminder.mjs"),
        str(Path(os.environ.get("PHONE_TEST_TYPESCRIPT", "all_day_recording_front/node_modules/typescript")).resolve()),
        str(phone / "phone/src/main/ets/v3"),
    ]
    run = subprocess.run(
        command,
        input=json.dumps(
            {
                "now": f.current_time.timestamp() * 1000,
                "event_id": result["event_id"],
                "responses": [first.as_dict(), second.as_dict()],
            }
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    print(run.stdout)


def test_speech_revision_and_independent_dimensions(people):
    f = people
    uid = _utterance_id(1)
    f.core.corrections.classify_segments(
        [{"utterance_id": uid, "revision": 1}], "non_speech"
    )
    a = assign(f, uid, "A")
    restored = rerun(f)
    assert restored.evidence["sound_kind"] == "non_speech"
    assert restored.evidence["person_annotation"]["person_id"] == a["person_id"]
    f.core.corrections.classify_segments(
        [{"utterance_id": restored.utterance_id, "revision": 1}], "speech"
    )
    final = rerun(f)
    assert final.evidence["sound_kind"] == "speech"
    assert sound_uses(final.evidence)["content_usable"]
    assert final.evidence["person_annotation"]["person_id"] == a["person_id"]
    with SqliteUnitOfWork(f.core.database) as uow:
        assert (
            uow.evidence.connection.execute(
                "SELECT count(*) FROM annotation_facts WHERE dimension='person'"
            ).fetchone()[0]
            == 1
        )
        assert (
            uow.evidence.connection.execute(
                "SELECT count(*) FROM annotation_facts WHERE dimension='sound'"
            ).fetchone()[0]
            == 2
        )


def test_split_partial_and_mixed_sound_mapping(people):
    f = people
    uid = _utterance_id(1)
    f.core.corrections.classify_segments(
        [{"utterance_id": uid, "revision": 1}], "non_speech"
    )
    split = rerun(f, end_ms=3000)
    assert split.evidence["sound_kind"] == "non_speech"
    # Partial correction cannot erase the untouched original range.
    f.core.corrections.classify_segments(
        [{"utterance_id": split.utterance_id, "revision": 1}], "speech"
    )
    assert rerun(f, end_ms=3000).evidence["sound_kind"] == "speech"
    assert rerun(f, start_ms=3000).evidence["sound_kind"] == "non_speech"
    mixed = rerun(f)
    assert "sound" in mixed.evidence["annotation_review"]["dimensions"]
    assert not sound_uses(mixed.evidence)["content_usable"]
    partial = rerun(f, start_ms=500, end_ms=2000)
    assert not sound_uses(partial.evidence)["content_usable"]
    assert "annotation_review" in partial.evidence


def test_true_concurrent_person_facts_are_not_last_writer_wins(people):
    f = people
    independent = rerun(f)  # Both devices saw no human predecessor.
    a = assign(f, _utterance_id(1), "A")
    b = assign(f, independent.utterance_id, "B")
    final = rerun(f)
    assert "person" in final.evidence["annotation_review"]["dimensions"]
    assert "person_annotation" not in final.evidence
    assert not sound_uses(final.evidence)["sample_candidate_allowed"]
    with SqliteUnitOfWork(f.core.database) as uow:
        heads = uow.evidence.overlapping_facts(
            uow.evidence.audio_evidence(final), "person"
        )
        assert {h["value"] for h in heads} == {a["person_id"], b["person_id"]}
    # Explicit review resolves only the observed conflict, with both predecessors.
    resolved = assign(f, final.utterance_id, "C")
    assert rerun(f).evidence["person_annotation"]["person_id"] == resolved["person_id"]


def test_late_old_projection_does_not_revive_and_mobile_payload_immutable(people):
    from allday_asr.v3.domain.device_sync import (
        ClientOperation,
        SyncRequest,
        PROJECTION_VERSION,
    )

    f = people
    uid = _utterance_id(1)
    assign(f, uid, "A")
    fresh = rerun(f)
    assign(f, fresh.utterance_id, "B")
    operation = ClientOperation(
        "late-old",
        "speaker.assign",
        None,
        {
            "selections": [{"utterance_id": uid, "revision": 2}],
            "display_name": "A-again",
        },
    )
    request = SyncRequest(PROJECTION_VERSION, None, (operation,), 500)
    first = f.core.mobile_sync.synchronize("device-1", request)
    assert first.receipts[0].status.value == "conflict"
    assert (
        f.core.mobile_sync.synchronize("device-1", request).receipts == first.receipts
    )
    with SqliteUnitOfWork(f.core.database) as uow:
        assert (
            uow.mobile_sync.find_operation("late-old").operation.payload
            == operation.payload
        )
        assert (
            uow.evidence.connection.execute(
                "SELECT count(*) FROM annotation_facts"
            ).fetchone()[0]
            == 2
        )


def test_fact_write_rolls_back_with_projection_failure(people):
    from allday_asr.v3.adapters.sqlite.evidence_projection_repository import (
        SqliteEvidenceProjectionRepository,
    )

    f = people
    assign(f, _utterance_id(1), "A")
    fresh = rerun(f)
    with patch.object(
        SqliteEvidenceProjectionRepository,
        "revise_utterance",
        side_effect=RuntimeError("injected write failure"),
    ):
        with pytest.raises(RuntimeError, match="injected"):
            assign(f, fresh.utterance_id, "B")
    assert (
        rerun(f).evidence["person_annotation"]["person_id"]
        == fresh.evidence["person_annotation"]["person_id"]
    )
    with SqliteUnitOfWork(f.core.database) as uow:
        assert (
            uow.evidence.connection.execute(
                "SELECT count(*) FROM annotation_facts"
            ).fetchone()[0]
            == 1
        )
        assert (
            uow.evidence.connection.execute(
                "SELECT count(*) FROM annotation_supersessions"
            ).fetchone()[0]
            == 0
        )


def test_raw_window_and_multiclip_samples_with_unrelated_a_preserved(people):
    import json

    f = people
    a = assign(f, _utterance_id(1), "A")
    candidate = f.core.people.list_review_candidates(a["person_id"])[0]
    f.core.people.review_prototype(
        candidate["prototype_id"], a["person_id"], "confirmed"
    )
    # Immutable historical raw-window and multi-clip fixtures are inserted, never rewritten.
    with f.core.database.transaction() as db:
        accepted = dict(
            db.execute(
                "SELECT * FROM voice_prototypes WHERE status='accepted'"
            ).fetchone()
        )
        original = json.loads(accepted["representative_clips_json"])
        for c in original:
            c["utterance_id"] = None
        unrelated = [
            {
                "media_id": original[0]["media_id"],
                "start_ms": 8000,
                "end_ms": 9000,
                "utterance_id": None,
            }
        ]
        for suffix, clips in [
            ("raw", original),
            ("multi", original + unrelated),
            ("unrelated", unrelated),
        ]:
            row = {
                **accepted,
                "prototype_id": "phase1-" + suffix,
                "source_prototype_id": None,
                "representative_clips_json": json.dumps(clips),
            }
            db.execute(
                "INSERT INTO voice_prototypes ("
                + ",".join(row)
                + ") VALUES ("
                + ",".join("?" for _ in row)
                + ")",
                tuple(row.values()),
            )
    restored = rerun(f)
    assign(f, restored.utterance_id, "B")
    with SqliteUnitOfWork(f.core.database) as uow:
        vectors = uow.people.person_vectors(f.provider.model, f.provider.model_version)
        assert len(vectors) == 1 and vectors[0][0] == a["person_id"]
        revoked = {
            r[0]
            for r in uow.evidence.connection.execute(
                "SELECT prototype_id FROM annotation_sample_revocations"
            )
        }
        assert {"phase1-raw", "phase1-multi"} <= revoked
        assert "phase1-unrelated" not in revoked
