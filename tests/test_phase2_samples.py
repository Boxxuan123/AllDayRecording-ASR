"""Real fact/queue/selection/review/query paths; synthetic audio and model only."""

from dataclasses import replace
from unittest.mock import patch
import numpy as np
import pytest
import soundfile as sf

from tests.test_phase1_human_facts import people as people, assign, rerun
from tests.test_v34_open_speaker_identity import _utterance_id
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.application.annotation_samples import AnnotationSampleWorker
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    SyncRequest,
    PROJECTION_VERSION,
)


def rows(f):
    with SqliteUnitOfWork(f.core.database) as u:
        return {
            r["utterance_id"]: u.evidence.get_utterance(r["utterance_id"])
            for r in u.evidence.connection.execute(
                "SELECT utterance_id FROM utterances"
            )
        }


def save(f, ids, pid):
    current = rows(f)
    return f.core.people.assign_utterances(
        [{"utterance_id": uid, "revision": current[uid].revision} for uid in ids],
        person_id=pid,
        display_name=None,
        actor="human",
    )


def classify(f, uid, kind):
    row = rows(f)[uid]
    return f.core.corrections.classify_segments(
        [{"utterance_id": uid, "revision": row.revision}], kind
    )


def short_rows(f, windows=((0, 3000), (3500, 6500), (7000, 10000))):
    original = rows(f)[_utterance_id(1)]
    with SqliteUnitOfWork(f.core.database) as u:
        db = u.evidence.connection
        db.execute("DELETE FROM utterances")
        ids = []
        for i, (start, end) in enumerate(windows):
            uid = stable_ulid("phase2-short", str(i))
            ids.append(uid)
            u.evidence.add_utterance(
                replace(
                    original, utterance_id=uid, ordinal=i, start_ms=start, end_ms=end
                )
            )
    return ids


@pytest.fixture
def audio(people):
    f = people
    seen = []

    class Backend:
        def extract_speaker_embeddings(self, samples, *, batch_size=8):
            seen.append([len(s) for s in samples])
            return np.tile(
                np.array([1.0, 0.0, 0.0], dtype=np.float32), (len(samples), 1)
            )

    provider = FunASRSpeakerEmbeddingProvider(
        f.core.audio_store, backend_factory=Backend, temp_root=f.root / "phase2-clips"
    )
    f.core.people._provider = provider

    def synthetic(source, destination, start_ms, end_ms):
        sf.write(
            destination, np.zeros((end_ms - start_ms) * 16, dtype=np.float32), 16000
        )
        return destination

    with patch(
        "allday_asr.v3.adapters.speaker_embeddings.funasr.extract_clip",
        side_effect=synthetic,
    ):
        yield f, seen, provider


@pytest.mark.parametrize("delivery", ["grouped", "split", "incremental"])
def test_delivery_equivalent_bounded_windows_and_retry(audio, delivery):
    f, seen, provider = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("Same")["person_id"]
    if delivery == "grouped":
        save(f, ids, pid)
    else:
        for i, uid in enumerate(ids):
            op = ClientOperation(
                stable_ulid("phase2-op", str(i)),
                "speaker.assign",
                None,
                {
                    "selections": [{"utterance_id": uid, "revision": 1}],
                    "person_id": pid,
                },
            )
            request = SyncRequest(PROJECTION_VERSION, None, (op,), 500)
            first = f.core.mobile_sync.synchronize("device-1", request)
            assert first.receipts[0].status.value == "applied"
            assert (
                f.core.mobile_sync.synchronize("device-1", request).receipts
                == first.receipts
            )
            if delivery == "incremental":
                f.core.people.sample_worker.run_pending()
    f.core.people.sample_worker.run_pending()
    candidates = f.core.people.list_review_candidates(pid)
    assert len(candidates) == 1
    assert candidates[0]["quality_score"] == 0.75
    assert seen[-1] == [
        48000,
        48000,
        48000,
    ]  # three synthetic clip inputs; the two gaps aren't embedded
    before = len(seen)
    for _ in range(3):
        f.core.people.process_annotation_samples([{"utterance_id": ids[0]}])
        f.core.people.sample_worker.run_pending()
    assert len(seen) == before
    f.core.people.review_prototype(candidates[0]["prototype_id"], pid, "confirmed")
    with SqliteUnitOfWork(f.core.database) as u:
        assert {
            p
            for p, _ in u.people.person_vectors(provider.model, provider.model_version)
        } == {pid}


def test_overlapping_audio_is_counted_once(audio):
    f, seen, _ = audio
    ids = short_rows(f, ((0, 3000), (2000, 5000), (4000, 7000)))
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids, pid)
    f.core.people.sample_worker.run_pending()
    assert seen == [[112000]]
    assert f.core.people.list_review_candidates(pid)[0][
        "quality_score"
    ] == pytest.approx(7 / 12)


def test_one_contributor_correction_withdraws_whole_vector_and_late_job(audio):
    f, _, provider = audio
    ids = short_rows(f)
    a = f.core.people.create_person("A")["person_id"]
    b = f.core.people.create_person("B")["person_id"]
    save(f, ids, a)
    f.core.people.sample_worker.run_pending()
    candidate = f.core.people.list_review_candidates(a)[0]
    f.core.people.review_prototype(candidate["prototype_id"], a, "confirmed")
    original = provider.embed

    def racing(tracks):
        computed = original(tracks)
        save(f, [ids[0]], b)
        return computed

    # A new model task starts from all 3 windows, then one changes during compute.
    with (
        patch.object(type(provider), "model_version", "synthetic-v2"),
        patch.object(provider, "embed", side_effect=racing),
    ):
        f.core.people.sample_worker.run_pending(limit=1)
        with SqliteUnitOfWork(f.core.database) as u:
            assert not u.people.person_vectors(provider.model, "v1-local")
            assert not u.evidence.connection.execute(
                "SELECT 1 FROM voice_prototypes WHERE model_version='synthetic-v2'"
            ).fetchone()
    # B may build from its own evidence, never inherit A's accepted whole vector.
    f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        assert not u.people.person_vectors(provider.model, provider.model_version)


def test_exclusion_restore_new_review_and_model_version(audio):
    f, _, provider = audio
    ids = short_rows(f)
    pid = f.core.people.create_person("A")["person_id"]
    save(f, ids, pid)
    f.core.people.sample_worker.run_pending()
    old = f.core.people.list_review_candidates(pid)[0]
    f.core.people.review_prototype(old["prototype_id"], pid, "confirmed")
    classify(f, ids[0], "non_speech")
    with SqliteUnitOfWork(f.core.database) as u:
        assert not u.people.person_vectors(provider.model, provider.model_version)
    classify(f, ids[0], "speech")
    f.core.people.sample_worker.run_pending()
    new = f.core.people.list_review_candidates(pid)[0]
    assert new["prototype_id"] != old["prototype_id"]
    with SqliteUnitOfWork(f.core.database) as u:
        assert not u.people.person_vectors(provider.model, provider.model_version)
    with patch.object(type(provider), "model_version", "synthetic-v2"):
        f.core.people.sample_worker.run_pending()
        status = f.core.people.annotation_status(ids)["items"]
        assert all(
            any(s["model_version"] == "synthetic-v2" for s in item["samples"])
            for item in status
        )
        assert not any(
            s["matching_eligible"] for item in status for s in item["samples"]
        )


def test_fact_receipt_nonblocking_failure_restart_and_retry(people):
    f = people
    pid = f.core.people.create_person("A")["person_id"]
    with patch.object(
        f.provider, "embed", side_effect=RuntimeError("synthetic model load failure")
    ) as model:
        save(f, [_utterance_id(1)], pid)
        assert model.call_count == 0
        assert (
            rows(f)[_utterance_id(1)].evidence["person_annotation"]["person_id"] == pid
        )
        assert f.core.people.sample_worker.run_pending()[0]["status"] == "retryable"
    assert (
        f.core.people.annotation_status([_utterance_id(1)])["items"][0][
            "processing_status"
        ]
        == "retryable"
    )
    # Reinstantiate the real worker as on service restart; durable retry survives.
    worker = AnnotationSampleWorker(f.core.people)
    with f.core.database.transaction() as db:
        db.execute("UPDATE annotation_sample_queue SET retry_at=0")
    assert worker.run_pending()[0]["status"] == "processed"
    assert f.core.people.list_review_candidates(pid)


def test_fact_and_queue_roll_back_together(people):
    f = people
    pid = f.core.people.create_person("A")["person_id"]
    from allday_asr.v3.adapters.sqlite.repositories import SqliteCorrectionRepository

    with patch.object(
        SqliteCorrectionRepository,
        "add",
        side_effect=RuntimeError("synthetic transaction fault"),
    ):
        with pytest.raises(RuntimeError):
            save(f, [_utterance_id(1)], pid)
    assert not rows(f)[_utterance_id(1)].evidence.get("person_annotation")
    with f.core.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM annotation_facts").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM annotation_sample_queue").fetchone()[0]
            == 0
        )


def test_unrelated_correct_sample_in_same_media_survives(people):
    f = people
    ids = short_rows(f)
    a = f.core.people.create_person("A")["person_id"]
    b = f.core.people.create_person("B")["person_id"]
    save(f, [ids[0]], a)
    f.core.people.sample_worker.run_pending()
    independent = f.core.people.list_review_candidates(a)[0]
    f.core.people.review_prototype(independent["prototype_id"], a, "confirmed")
    save(f, ids[1:], a)
    f.core.people.sample_worker.run_pending()
    mixed = f.core.people.list_review_candidates(a)[0]
    f.core.people.review_prototype(mixed["prototype_id"], a, "confirmed")
    save(f, [ids[1]], b)
    with SqliteUnitOfWork(f.core.database) as u:
        assert (
            len(u.people.person_vectors(f.provider.model, f.provider.model_version))
            == 1
        )
    status = f.core.people.annotation_status([ids[0]])["items"][0]
    assert sum(s["matching_eligible"] for s in status["samples"]) == 1


def test_rerun_and_irrelevant_text_revision_do_not_rebuild(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    before = f.core.people.list_review_candidates(a["person_id"])[0]["prototype_id"]
    rerun(f)
    f.core.people.sample_worker.run_pending()
    from allday_asr.v3.application import CorrectUtteranceCommand

    row = rows(f)[_utterance_id(1)]
    f.core.corrections.correct_utterance(
        CorrectUtteranceCommand(
            row.utterance_id, row.revision, "Only text changed", "human"
        )
    )
    f.core.people.sample_worker.run_pending()
    assert (
        f.core.people.list_review_candidates(a["person_id"])[0]["prototype_id"]
        == before
    )
    with f.core.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM annotation_facts").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM annotation_sample_sets").fetchone()[0] == 1
        )


def test_inactive_source_track_is_not_a_fact_eligibility_gate(people):
    f = people
    pid = f.core.people.create_person("A")["person_id"]
    save(f, [_utterance_id(1)], pid)
    with f.core.database.transaction() as db:
        db.execute("UPDATE speaker_cluster_memberships SET state='removed'")
    assert f.core.people.sample_worker.run_pending()[0]["status"] == "processed"
    assert f.core.people.list_review_candidates(pid)


def test_matching_status_and_query_obey_quality_policy(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    candidate = f.core.people.list_review_candidates(a["person_id"])[0]
    f.core.people.review_prototype(
        candidate["prototype_id"], a["person_id"], "confirmed"
    )
    with SqliteUnitOfWork(f.core.database) as u:
        policy = u.people.identity_policy(a["person_id"])
        u.people.add_identity_policy_revision(
            a["person_id"],
            maturity_status=policy["maturity_status"],
            auto_match_enabled=False,
            suggest_threshold=0.82,
            auto_accept_threshold=0.92,
            minimum_margin=0.05,
            minimum_quality=0.99,
            calibration={},
            actor="human",
            created_at=f.core.people._now().isoformat(),
        )
        assert not u.people.person_vectors(f.provider.model, f.provider.model_version)
    status = f.core.people.annotation_status([_utterance_id(1)])["items"][0]
    assert not any(s["matching_eligible"] for s in status["samples"])
    assert status["processing_status"] == "accepted_unusable"


def test_missing_audio_and_bounded_model_failures(people):
    f = people
    pid = f.core.people.create_person("A")["person_id"]
    save(f, [_utterance_id(1)], pid)
    with patch.object(
        f.provider, "embed", side_effect=FileNotFoundError("synthetic absent audio")
    ):
        assert f.core.people.sample_worker.run_pending()[0]["status"] == "missing_audio"
        assert f.core.people.sample_worker.run_pending() == []
    f.core.people.process_annotation_samples([{"utterance_id": _utterance_id(1)}])
    with patch.object(
        f.provider, "embed", side_effect=RuntimeError("synthetic model error")
    ) as model:
        for _ in range(5):
            with f.core.database.transaction() as db:
                db.execute("UPDATE annotation_sample_queue SET retry_at=0")
            f.core.people.sample_worker.run_pending()
        assert model.call_count == 3
    assert (
        f.core.people.annotation_status([_utterance_id(1)])["items"][0][
            "processing_status"
        ]
        == "retryable"
    )
