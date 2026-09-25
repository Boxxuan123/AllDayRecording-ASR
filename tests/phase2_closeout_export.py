"""Actual server exports for native-SQLite legacy receipt recovery probes."""

import json
import sys
from dataclasses import replace
from unittest.mock import patch
from allday_asr.v3.adapters.sqlite.sync_repositories import SqliteMobileSyncRepository
from pathlib import Path
from tests.test_v34_open_speaker_identity import V34OpenSpeakerIdentityTests
from tests.test_phase2_samples import short_rows, rows, classify
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.device_sync import (
    ClientOperation,
    SyncRequest,
    PROJECTION_VERSION,
)


def export():
    f = V34OpenSpeakerIdentityTests()
    f.setUp()
    f._seed_track(1)
    try:
        ids = short_rows(f)[:2]
        row = rows(f)[ids[0]]
        with SqliteUnitOfWork(f.core.database) as u:
            u.changes.append(
                "recording_session",
                row.session_id,
                1,
                "upsert",
                {
                    "session_id": row.session_id,
                    "captured_start": row.start_at.isoformat(),
                    "state": "ready_for_processing",
                    "revision": 1,
                },
            )
        for label in ["live_speech", "speech", "live_speech", "speech"]:
            classify(f, ids[1], label)
        current = rows(f)
        payload = {
            "selections": [
                {"utterance_id": uid, "revision": current[uid].revision} for uid in ids
            ],
            "sound_kind": "non_speech",
        }
        op = ClientOperation(
            stable_ulid("closeout-legacy"), "segment.classify", None, payload
        )
        req = SyncRequest(PROJECTION_VERSION, None, (op,), 500)
        original = SqliteMobileSyncRepository.record_operation

        def legacy_record(repo, device_id, operation, digest, receipt):
            return original(
                repo,
                device_id,
                operation,
                digest,
                replace(receipt, resource_results=None),
            )

        # Fixture-only legacy serialization; real operation execution is unchanged.
        with patch.object(
            SqliteMobileSyncRepository, "record_operation", legacy_record
        ):
            response = f.core.mobile_sync.synchronize("device-1", req).as_dict()
        assert [r["revision"] for r in response["receipts"][0]["resource_results"]] == [
            2,
            6,
        ]
        # Model a schema9 device with an already accepted multi-selection row,
        # and the corresponding pre-extension server receipt. Payload never changes.
        with SqliteUnitOfWork(f.core.database) as u:
            db = u.people.connection
            before = dict(
                db.execute(
                    "SELECT * FROM client_operations WHERE operation_id=?",
                    (op.operation_id,),
                ).fetchone()
            )
            legacy = json.loads(before["receipt_json"])
            assert "resource_results" not in legacy
        classify(f, ids[0], "speech")
        late = f.core.mobile_sync.synchronize(
            "device-1",
            SyncRequest(PROJECTION_VERSION, response["next_cursor"], (), 500),
        ).as_dict()
        recovered = f.core.mobile_sync.synchronize(
            "device-1", SyncRequest(PROJECTION_VERSION, late["next_cursor"], (op,), 500)
        ).as_dict()
        repeated = f.core.mobile_sync.synchronize(
            "device-1",
            SyncRequest(PROJECTION_VERSION, recovered["next_cursor"], (op,), 500),
        ).as_dict()
        assert recovered["receipts"] == repeated["receipts"] and not repeated["changes"]
        assert [
            r["revision"] for r in recovered["receipts"][0]["resource_results"]
        ] == [3, 6]
        assert rows(f)[ids[0]].evidence["sound_kind"] == "speech"
        with SqliteUnitOfWork(f.core.database) as u:
            after = dict(
                u.people.connection.execute(
                    "SELECT * FROM client_operations WHERE operation_id=?",
                    (op.operation_id,),
                ).fetchone()
            )
            for field in (
                "payload_json",
                "payload_sha256",
                "created_at",
                "completed_at",
                "receipt_json",
            ):
                assert before[field] == after[field]
        noop_payload = {
            "selections": [
                {"utterance_id": uid, "revision": rows(f)[uid].revision}
                for uid in ids[1:]
            ],
            "sound_kind": "non_speech",
        }
        noop = ClientOperation(
            stable_ulid("closeout-noop"), "segment.classify", None, noop_payload
        )
        nr = f.core.mobile_sync.synchronize(
            "device-1",
            SyncRequest(PROJECTION_VERSION, recovered["next_cursor"], (noop,), 500),
        ).as_dict()
        assert nr["receipts"][0]["resource_results"][0]["revision"] == 6
        assert not nr["changes"]
        return {
            "operation": {
                "operation_id": op.operation_id,
                "kind": op.kind,
                "base_revision": None,
                "payload": payload,
            },
            "response": response,
            "late_response": late,
            "recovery_response": recovered,
            "repeat_response": repeated,
            "legacy_receipt": legacy,
            "noop_response": nr,
            "noop_operation": {
                "operation_id": noop.operation_id,
                "kind": noop.kind,
                "base_revision": None,
                "payload": noop_payload,
            },
            "corrected_uid": ids[0],
            "actual_revisions": {ids[0]: 2, ids[1]: 6},
        }
    finally:
        f.tearDown()


if __name__ == "__main__":
    data = export()
    Path(sys.argv[1]).write_text(json.dumps(data), encoding="utf8")
    print(
        "PASS actual per-resource results, no-op, legacy recovery/replay and immutable payload/digest"
    )
