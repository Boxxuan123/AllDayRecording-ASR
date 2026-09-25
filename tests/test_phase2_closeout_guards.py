"""Closeout guards use real facts, reviews, worker and matching repositories."""

from unittest.mock import patch
import numpy as np
import pytest
import soundfile as sf
from tests.test_phase1_human_facts import people as people, reminders as reminders
from tests.test_phase2_samples import audio as audio, short_rows, save, classify
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application.annotation_samples import AnnotationSampleWorker
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.adapters.audio.tools import extract_clip


def snapshot(f):
    with SqliteUnitOfWork(f.core.database) as u:
        return [
            tuple(r)
            for r in u.people.connection.execute(
                "SELECT * FROM annotation_facts ORDER BY fact_id"
            )
        ]


@pytest.mark.parametrize("review", ["pending", "confirmed", "rejected", "retracted"])
def test_subset_return_preserves_review_and_restart(audio, review):
    f, seen, provider = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids[:2], pid)
    f.core.people.sample_worker.run_pending()
    first = f.core.people.list_review_candidates(pid)[0]["prototype_id"]
    if review == "retracted":
        f.core.people.review_prototype(first, pid, "confirmed")
    if review != "pending":
        f.core.people.review_prototype(first, pid, review)
    facts = snapshot(f)
    save(f, ids[2:], pid)
    f.core.people.sample_worker.run_pending()
    classify(f, ids[2], "non_speech")
    f.core.people.sample_worker.run_pending()
    before = len(seen)
    for _ in range(3):
        f.core.people.process_annotation_samples([{"utterance_id": ids[0]}])
        AnnotationSampleWorker(f.core.people).run_pending()
    assert len(seen) == before
    current = f.core.people.list_review_candidates(pid, None)
    assert [c["prototype_id"] for c in current] == [first]
    assert current[0]["review_status"] == review
    assert bool(f.core.people.list_review_candidates(pid)) == (review == "pending")
    with SqliteUnitOfWork(f.core.database) as u:
        assert len(u.people.person_vectors(provider.model, provider.model_version)) == (
            review == "confirmed"
        )
        assert (
            u.people.connection.execute(
                "SELECT count(*) FROM annotation_sample_sets WHERE current=1"
            ).fetchone()[0]
            == 1
        )
    assert {r[0] for r in facts} <= {r[0] for r in snapshot(f)}
    # Repeated S1 -> expanded evidence -> S1; sound authorization changes only third clip.
    for _ in range(2):
        classify(f, ids[2], "speech")
        f.core.people.sample_worker.run_pending()
        classify(f, ids[2], "non_speech")
        f.core.people.sample_worker.run_pending()
        assert (
            f.core.people.list_review_candidates(pid, None)[0]["prototype_id"] == first
        )
        assert (
            f.core.people.list_review_candidates(pid, None)[0]["review_status"]
            == review
        )


def test_history_retract_via_phone_preserves_other_grants_and_facts(audio):
    f, _, provider = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids[:2], pid)
    f.core.people.sample_worker.run_pending()
    first = f.core.people.list_review_candidates(pid)[0]["prototype_id"]
    f.core.people.review_prototype(first, pid, "confirmed", actor="original-reviewer")
    save(f, ids[2:], pid)
    f.core.people.sample_worker.run_pending()
    second = f.core.people.list_review_candidates(pid)[0]["prototype_id"]
    f.core.people.review_prototype(second, pid, "confirmed")
    facts = snapshot(f)
    service = DeviceReviewService(f.core)
    item = next(
        i
        for i in service.snapshot()["items"]
        if i["context"].get("voice_mode") == "accepted_grant"
        and first in i["context"]["prototype_ids"]
    )
    wrong = f.core.people.create_person("Other")["person_id"]
    with pytest.raises(ValueError):
        f.core.people.review_prototype(first, wrong, "retracted")
    with pytest.raises((ValueError, KeyError)):
        f.core.people.review_prototype("invalid", pid, "retracted")
    with pytest.raises((ValueError, KeyError)):
        f.core.people.review_prototype(first, pid, "confirmed")
    service.resolve(
        "device-1",
        {"review_id": item["review_id"], "action": "retract", "prototype_id": first},
    )
    with pytest.raises(ValueError):
        f.core.people.review_prototype(first, pid, "retracted")
    assert snapshot(f) == facts
    with SqliteUnitOfWork(f.core.database) as u:
        assert len(u.people.person_vectors(provider.model, provider.model_version)) == 1
        history = u.people.connection.execute(
            "SELECT decision,actor FROM voice_prototype_reviews WHERE prototype_id=? ORDER BY created_at,review_id",
            (first,),
        ).fetchall()
        assert [tuple(r) for r in history] == [
            ("confirmed", "original-reviewer"),
            ("retracted", "phone-device:device-1"),
        ]
    assert [
        c["prototype_id"]
        for c in f.core.people.list_review_candidates(pid, "confirmed")
    ] == [second]


def test_old_model_and_revoked_sources_cannot_be_confirmed(audio):
    f, _, provider = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids, pid)
    f.core.people.sample_worker.run_pending()
    old = f.core.people.list_review_candidates(pid)[0]["prototype_id"]
    with patch.object(type(provider), "model_version", "v2"):
        with pytest.raises(ValueError, match="模型"):
            f.core.people.review_prototype(old, pid, "confirmed")
        f.core.people.sample_worker.run_pending()
        fresh = [
            c
            for c in f.core.people.list_review_candidates(pid)
            if c["prototype_id"] != old
        ][0]
        classify(f, ids[0], "non_speech")
        with pytest.raises(KeyError):
            f.core.people.review_prototype(fresh["prototype_id"], pid, "confirmed")


WINDOWS = (
    (0, 800),
    (900, 1700),
    (1800, 2600),
    (2700, 3500),
    (3600, 4400),
    (4500, 10000),
)


@pytest.mark.parametrize(
    "order", [list(range(6)), list(reversed(range(6))), [3, 5, 0, 4, 2, 1], None]
)
def test_longest_selection_delivery_independent_and_withdrawal(audio, order):
    f, seen, provider = audio
    ids = short_rows(f, WINDOWS)
    pid = f.core.people.create_person("A")["person_id"]
    if order is None:
        save(f, ids, pid)
    else:
        for i in order:
            save(f, [ids[i]], pid)
            f.core.people.sample_worker.run_pending()
    f.core.people.sample_worker.run_pending()
    c = f.core.people.list_review_candidates(pid)[0]
    assert c["quality_score"] == pytest.approx(8.7 / 12)
    assert [(w["start_ms"], w["end_ms"]) for w in c["representative_clips"]] == list(
        WINDOWS[:4]
    ) + [WINDOWS[5]]
    assert seen[-1] == [12800] * 4 + [88000]
    f.core.people.review_prototype(c["prototype_id"], pid, "confirmed")
    classify(f, ids[5], "non_speech")
    with SqliteUnitOfWork(f.core.database) as u:
        assert not u.people.person_vectors(provider.model, provider.model_version)
    f.core.people.sample_worker.run_pending()
    assert not f.core.people.list_review_candidates(pid)


def test_overlap_and_tiny_tail_do_not_inflate_selected_score(audio):
    f, seen, _ = audio
    ids = short_rows(f, ((0, 6500), (2000, 6400), (6500, 6600), (7000, 7100)))
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids[:3], pid)
    f.core.people.sample_worker.run_pending()
    first = f.core.people.list_review_candidates(pid)[0]
    save(f, ids[3:], pid)
    f.core.people.sample_worker.run_pending()
    assert (
        f.core.people.list_review_candidates(pid)[0]["prototype_id"]
        == first["prototype_id"]
    )
    assert seen == [[105600]]
    assert first["quality_score"] == pytest.approx(6.6 / 12)


def test_selection_version_enqueues_old_database_once(audio):
    f, seen, _ = audio
    ids = short_rows(f, WINDOWS)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids, pid)
    f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        u.people.connection.execute(
            "UPDATE annotation_sample_runtime SET model_key='old-selection-v1'"
        )
    assert (
        AnnotationSampleWorker(f.core.people).run_pending()[0]["status"] == "processed"
    )
    assert len(seen) == 1
    assert AnnotationSampleWorker(f.core.people).run_pending() == []


def test_selection_and_completion_rollback_together(audio):
    f, _, _ = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids[:2], pid)
    f.core.people.sample_worker.run_pending()
    first = f.core.people.list_review_candidates(pid)[0]["prototype_id"]
    save(f, ids[2:], pid)
    f.core.people.sample_worker.run_pending()
    classify(f, ids[2], "non_speech")
    from allday_asr.v3.adapters.sqlite.annotation_sample_repository import (
        AnnotationSampleRepositoryMixin,
    )

    with patch.object(
        AnnotationSampleRepositoryMixin,
        "finish_sample_job",
        side_effect=RuntimeError("transaction failure"),
    ):
        with pytest.raises(RuntimeError):
            f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        assert (
            u.people.connection.execute(
                "SELECT current FROM annotation_sample_sets WHERE prototype_id=?",
                (first,),
            ).fetchone()[0]
            == 0
        )
        u.people.connection.execute("UPDATE annotation_sample_queue SET lease_until=0")
    AnnotationSampleWorker(f.core.people).run_pending()
    assert f.core.people.list_review_candidates(pid)[0]["prototype_id"] == first


def test_actual_extract_clip_on_synthetic_wav(tmp_path):
    # No extract_clip patch: ffmpeg reads a known waveform with distinct regions.
    rate = 16000
    t = np.arange(rate * 10) / rate
    samples = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    samples[rate * 3 : rate * 5] = 0
    source = tmp_path / "synthetic.wav"
    sf.write(source, samples, rate, subtype="PCM_16")
    for start, end in [(900, 1700), (3600, 4400), (4500, 10000)]:
        output = extract_clip(source, tmp_path / f"clip-{start}.wav", start, end)
        actual, sr = sf.read(output, dtype="float32")
        assert sr == rate and len(actual) == (end - start) * 16
        np.testing.assert_allclose(actual, samples[start * 16 : end * 16], atol=7e-5)


def test_receipt_migration_atomic_and_legacy_recovery_idempotent(people):
    import sqlite3
    from dataclasses import replace
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner

    f = people
    with f.core.database.transaction() as db:
        facts = [tuple(r) for r in db.execute("SELECT * FROM annotation_facts")]
        db.execute("DROP TABLE annotation_receipt_recovery")
        db.execute("DELETE FROM schema_migrations WHERE version=15")
    broken = replace(
        MIGRATIONS[-1], sql=MIGRATIONS[-1].sql + "\nSELECT invalid_upgrade;"
    )
    with pytest.raises(sqlite3.OperationalError):
        V3MigrationRunner(
            f.core.paths.database_path, migrations=(*MIGRATIONS[:-1], broken)
        ).initialize()
    with f.core.database.transaction() as db:
        assert (
            db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 14
        )
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='annotation_receipt_recovery'"
        ).fetchone()
    assert f.core.initialize() == 15 and f.core.initialize() == 15
    with f.core.database.transaction() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM annotation_facts")] == facts
    from tests.phase2_closeout_export import export

    assert export()["recovery_response"]["receipts"][0]["resource_results"]


def test_legacy_deleted_resource_recovery_is_explicit_tombstone(people):
    from allday_asr.v3.domain.ids import stable_ulid
    from allday_asr.v3.domain.device_sync import (
        ClientOperation,
        OperationReceipt,
        ClientOperationStatus,
        SyncRequest,
        PROJECTION_VERSION,
    )
    from allday_asr.v3.application.mobile_sync import _operation_sha256

    f = people
    uid = stable_ulid("deleted-resource")
    op = ClientOperation(
        stable_ulid("deleted-op"),
        "segment.classify",
        None,
        {
            "selections": [{"utterance_id": uid, "revision": 7}],
            "sound_kind": "non_speech",
        },
    )
    with SqliteUnitOfWork(f.core.database) as u:
        u.changes.append("utterance", uid, 8, "tombstone", None)
        u.mobile_sync.record_operation(
            "device-1",
            op,
            _operation_sha256(op),
            OperationReceipt(op.operation_id, ClientOperationStatus.APPLIED, 8, None),
        )
    r = f.core.mobile_sync.synchronize(
        "device-1", SyncRequest(PROJECTION_VERSION, None, (op,), 500)
    )
    target = r.receipts[0].resource_results[0]
    assert target == {"resource_id": uid, "revision": 9}
    assert any(
        c.resource_id == uid and c.revision == 9 and c.operation == "tombstone"
        for c in r.changes
    )
    again = f.core.mobile_sync.synchronize(
        "device-1", SyncRequest(PROJECTION_VERSION, r.next_cursor, (op,), 500)
    )
    assert again.receipts == r.receipts and not again.changes


def test_sample_retraction_does_not_change_confirmed_reminder(reminders):
    from dataclasses import replace
    from tests.test_v34_open_speaker_identity import FakeEmbeddingProvider
    from tests.test_v33_intelligent_reminders import _intent, UTTERANCE_ID
    from allday_asr.v3.application import SpeakerIdentityService

    f = reminders
    provider = FakeEmbeddingProvider({})
    service = SpeakerIdentityService(f.factory, provider, f.knowledge)
    embed = provider.embed

    def fixed(tracks):
        for track in tracks:
            provider.vectors[track.speaker_track_id] = (1.0, 0.0, 0.0)
        return embed(tracks)

    with patch.object(provider, "embed", side_effect=fixed):
        person = service.assign_utterances(
            [{"utterance_id": UTTERANCE_ID, "revision": 1}],
            person_id=None,
            display_name="A",
            actor="human",
        )
        service.sample_worker.run_pending()
    pid = person["person_id"]
    prototype = service.list_review_candidates(pid)[0]["prototype_id"]
    service.review_prototype(prototype, pid, "confirmed")
    before = f._confirmed(replace(_intent(), actor_person_id=pid))["reminder"]
    with f.factory() as u:
        facts = [
            tuple(r)
            for r in u.people.connection.execute("SELECT * FROM annotation_facts")
        ]
        # Historical selection state only; correct human facts and grant untouched.
        u.people.connection.execute("UPDATE annotation_sample_sets SET current=0")
    service.review_prototype(prototype, pid, "retracted")
    after = f.reminders.schedule(before["event_id"])
    assert {k: after[k] for k in ("status", "title", "scheduled_at")} == {
        k: before[k] for k in ("status", "title", "scheduled_at")
    }
    with f.factory() as u:
        assert [
            tuple(r)
            for r in u.people.connection.execute("SELECT * FROM annotation_facts")
        ] == facts
        assert not u.people.person_vectors(provider.model, provider.model_version)
