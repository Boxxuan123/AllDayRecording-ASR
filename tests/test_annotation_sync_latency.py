import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from tests.test_phase1_human_facts import people as people
from tests.test_phase2_samples import audio as audio
from tests.annotation_sync_fixture import seed
from tests.test_v3_device_sync import _b64
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.annotation_sample_snapshot import (
    load_snapshot,
)
from allday_asr.v3.adapters.sqlite.annotation_sample_plan import compute_plans
from allday_asr.v3.adapters.transfer.trust import TransferDeviceTrustAdapter
from allday_asr.v3.interfaces.transfer.devices import (
    DeviceCredentialStore,
    DeviceAuthManager,
    DEVICE_ALGORITHM,
    build_device_signature_payload,
    DeviceUnauthorizedError,
)
from allday_asr.v3.interfaces.transfer.passkeys import RequestBinding
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    SyncRequest,
    PROJECTION_VERSION,
)
from allday_asr.v3.domain.ids import new_ulid


def auth(f):
    key = ec.generate_private_key(ec.SECP256R1())
    manager = DeviceAuthManager(
        DeviceCredentialStore(f.root / "diagnostic-devices.json")
    )
    record = manager.store.register(
        device_name="Synthetic",
        algorithm=DEVICE_ALGORITHM,
        public_key=_b64(
            key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ),
        passkey_credential_id="synthetic",
    )
    trust = TransferDeviceTrustAdapter(
        manager, lambda: SqliteUnitOfWork(f.core.database), "a" * 64
    )
    trust.enroll(record)

    def verify():
        binding = RequestBinding.for_request(
            method="POST", path="/device/v3/sync", body=b"{}"
        )
        challenge = trust.start_authentication(
            device_id=record.device_id, binding=binding
        )
        signature = key.sign(
            build_device_signature_payload(
                challenge_id=challenge["challenge_id"],
                nonce=challenge["nonce"],
                binding=binding,
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        return trust.verify_request(
            device_id=record.device_id,
            challenge_id=challenge["challenge_id"],
            encoded_signature=_b64(signature),
            binding=binding,
        )

    return trust, record, verify


@pytest.mark.parametrize("size", [32, 1800])
def test_detached_real_planner_allows_full_auth_and_annotation(audio, size):
    f, seen, _ = audio
    sid, pid, ids = seed(f, size)
    trust, record, verify = auth(f)
    entered, release = threading.Event(), threading.Event()

    def held(snapshot, model, version):
        entered.set()
        assert release.wait(8)
        return compute_plans(snapshot, model, version)

    with (
        patch(
            "allday_asr.v3.application.annotation_samples.compute_plans",
            side_effect=held,
        ),
        ThreadPoolExecutor(2) as pool,
    ):
        worker = pool.submit(f.core.people.sample_worker.run_pending, 1)
        try:
            assert entered.wait(3)

            def independent():
                verify()
                f.core.corrections.classify_segments(
                    [{"utterance_id": ids[-1], "revision": 1}], "non_speech"
                )

            pool.submit(independent).result(timeout=2)
            assert not worker.done()
        finally:
            release.set()
        assert worker.result(timeout=4)[0]["status"] == "retryable"
    # The stale plan never becomes current, then the changed generation can finish.
    with f.core.database.transaction() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM annotation_sample_sets WHERE current=1"
            ).fetchone()[0]
            == 0
        )
        db.execute("UPDATE annotation_sample_queue SET retry_at=0")
    result = f.core.people.sample_worker.run_pending(1)
    assert result[0]["status"] == "processed"
    assert seen
    with f.core.database.read() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM annotation_sample_sets WHERE current=1"
            ).fetchone()[0]
            > 0
        )
    trust.revoke(record.device_id, actor="test", reason="synthetic")
    with pytest.raises(DeviceUnauthorizedError):
        verify()


@pytest.mark.parametrize(
    "mutation",
    ["fact", "projection", "replica", "link", "generation", "lease", "model"],
)
def test_publication_rejects_changed_inputs(audio, mutation):
    f, _, provider = audio
    sid, pid, ids = seed(f, 32)
    original = provider.embed

    def mutate(tracks):
        result = original(tracks)
        with f.core.database.transaction() as db:
            if mutation == "fact":
                db.execute(
                    "UPDATE annotation_facts SET state='conflict' WHERE fact_id=(SELECT fact_id FROM annotation_facts LIMIT 1)"
                )
            elif mutation == "projection":
                db.execute(
                    "UPDATE utterances SET evidence_json='{}' WHERE utterance_id=?",
                    (ids[0],),
                )
            elif mutation == "replica":
                db.execute("UPDATE audio_replicas SET state='missing'")
            elif mutation == "link":
                db.execute("UPDATE speaker_tracks SET label='changed'")
            elif mutation == "generation":
                db.execute("UPDATE annotation_sample_queue SET generation=generation+1")
            elif mutation == "lease":
                db.execute("UPDATE annotation_sample_queue SET token='another-worker'")
        if mutation == "model":
            f.core.people._provider = type(
                "Provider", (), {"model": "different", "model_version": "v2"}
            )()
        return result

    with patch.object(provider, "embed", side_effect=mutate):
        f.core.people.sample_worker.run_pending(1)
    with f.core.database.read() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM annotation_sample_sets").fetchone()[0] == 0
        )


def test_projection_parsed_once_and_person_queries_are_targeted(people):
    f = people
    sid, pid, ids = seed(f, 1800)
    with f.core.database.read() as db:
        statements = []
        db.set_trace_callback(statements.append)
        snap = load_snapshot(db, sid)
    assert len([s for s in statements if s.startswith("SELECT")]) == 6
    with patch(
        "allday_asr.v3.adapters.sqlite.annotation_sample_snapshot.json.loads",
        wraps=__import__("json").loads,
    ) as loads:
        compute_plans(snap, "fixed", "1")
        assert loads.call_count == len(snap.facts) * 2 + len(snap.projections)
    from allday_asr.v3.adapters.sqlite.people_repository import SqlitePeopleRepository

    with (
        patch.object(
            SqlitePeopleRepository,
            "list_people",
            side_effect=AssertionError("heavy query"),
        ),
        patch.object(
            SqlitePeopleRepository,
            "list_review_candidates",
            side_effect=AssertionError("heavy query"),
        ),
    ):
        ops = tuple(
            ClientOperation(
                new_ulid(),
                "speaker.assign",
                None,
                {
                    "person_id": pid,
                    "selections": [{"utterance_id": uid, "revision": 1}],
                },
            )
            for uid in ids[:32]
        )
        result = f.core.mobile_sync.synchronize(
            "device-1", SyncRequest(PROJECTION_VERSION, None, ops, 500)
        )
        assert all(r.status.value == "applied" for r in result.receipts)
        assert (
            f.core.mobile_sync.synchronize(
                "device-1", SyncRequest(PROJECTION_VERSION, None, ops, 500)
            ).receipts
            == result.receipts
        )
        assert any(p["person_id"] == pid for p in f.core.people.people_choices())


def test_legacy_equivalence_and_cross_session_new_conflict(people):
    from tests.annotation_plan_baseline import plans as baseline
    from allday_asr.v3.adapters.sqlite.evidence_projection_repository import (
        SqliteEvidenceProjectionRepository,
    )

    f = people
    sid, pid, ids = seed(f, 32)
    with f.core.database.read() as db:
        snap = load_snapshot(db, sid)
        expected = baseline(
            db,
            SqliteEvidenceProjectionRepository(db, now=lambda: ""),
            sid,
            "fixed",
            "1",
        )
    from dataclasses import asdict

    actual = compute_plans(snap, "fixed", "1")
    assert [asdict(p) for p in actual[0]] == [asdict(p) for p in expected[0]]
    assert actual[1] == expected[1]
    f._seed_track(3)
    with SqliteUnitOfWork(f.core.database) as u:
        from tests.test_v34_open_speaker_identity import _utterance_id

        other = u.evidence.get_utterance(_utterance_id(3))
        u.evidence.insert_fact(
            new_ulid(),
            "sound",
            "media_speech",
            other,
            [dict(media_id="media-2", start_ms=0, end_ms=32000)],
            {},
            "synthetic",
            "2026-09-27T00:00:00Z",
        )
    with f.core.database.read() as db:
        fresh = load_snapshot(db, sid)
        expected = baseline(
            db,
            SqliteEvidenceProjectionRepository(db, now=lambda: ""),
            sid,
            "fixed",
            "1",
        )
    assert fresh.revision != snap.revision
    assert compute_plans(fresh, "fixed", "1") == expected == ((), ["source_excluded"])

    # Human facts outlive their source projection. Missing source rows cannot hide conflicts.
    with f.core.database.transaction() as db:
        db.execute("DELETE FROM utterances WHERE utterance_id=?", (other.utterance_id,))
    with f.core.database.read() as db:
        detached = load_snapshot(db, sid)
        expected = baseline(db, SqliteEvidenceProjectionRepository(db, now=lambda: ""), sid, "fixed", "1")
    assert compute_plans(detached, "fixed", "1") == expected == ((), ["source_excluded"])
