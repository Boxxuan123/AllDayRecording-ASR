"""Line-based network substitute for the phone end-to-end engineering test.

Only synthetic fixtures and audio/model are used. The real sync, fact ledger,
durable worker, review service, reminder service and matching query run here.
"""

import json
import sys
import traceback
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from tests.test_v34_open_speaker_identity import V34OpenSpeakerIdentityTests, NOW_TEXT
from tests.test_phase2_samples import short_rows, audio, rows
from tests.test_v33_intelligent_reminders import _intent, _submission
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.contracts import utterance_dto
from allday_asr.v3.domain.device_sync import ClientOperation, SyncRequest
from allday_asr.v3.interfaces.device_annotations import DeviceAnnotationService
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService


def main():
    f = V34OpenSpeakerIdentityTests()
    f.setUp()
    f._seed_track(1)
    generator = audio.__wrapped__(f)
    _, _, provider = next(generator)
    ids = short_rows(f)
    shadow_id = "00000000000000000000000999"
    initial_template = rows(f)[ids[0]]
    with SqliteUnitOfWork(f.core.database) as u:
        u.evidence.add_utterance(
            replace(initial_template, utterance_id=shadow_id, ordinal=99)
        )
    pid = f.core.people.create_person("Same display name")["person_id"]
    second = f.core.people.create_person("Same display name")["person_id"]
    session_id = rows(f)[ids[0]].session_id
    initial_rows = rows(f)
    with SqliteUnitOfWork(f.core.database) as u:
        u.changes.append(
            "recording_session",
            session_id,
            1,
            "upsert",
            {
                "session_id": session_id,
                "captured_start": NOW_TEXT,
                "state": "ready_for_processing",
                "revision": 1,
            },
        )
        for row in (initial_rows[uid] for uid in ids):
            u.changes.append(
                "utterance",
                row.utterance_id,
                row.revision,
                "upsert",
                utterance_dto(
                    row,
                    speaker_label=u.evidence.speaker_label(row.speaker_track_id),
                    original_speaker_label=u.evidence.speaker_label(
                        row.original_speaker_track_id
                    ),
                ),
            )
    reminder = None

    def command(message):
        nonlocal reminder
        action = message["action"]
        if action == "init":
            return {
                "ids": ids,
                "person_id": pid,
                "second_person_id": second,
                "now": datetime.now(timezone.utc).timestamp() * 1000,
            }
        if action == "sync":
            req = message["request"]
            ops = tuple(
                ClientOperation(
                    v["operation_id"], v["kind"], v["base_revision"], v["payload"]
                )
                for v in req["client_operations"]
            )
            # Single-row pull pages deliberately separate receipts and projections.
            return f.core.mobile_sync.synchronize(
                "device-1",
                SyncRequest(req["projection_version"], req["cursor"], ops, 1),
            ).as_dict()
        if action == "annotations":
            return DeviceAnnotationService(f.core).execute(
                "device-1", message["request"]
            )
        if action == "reviews":
            return DeviceReviewService(f.core).snapshot()
        if action == "resolve":
            return DeviceReviewService(f.core).resolve("device-1", message["request"])
        if action == "worker":
            return f.core.people.sample_worker.run_pending()
        if action == "conflict":
            shadow = rows(f)[shadow_id]
            return f.core.people.assign_utterances(
                [{"utterance_id": shadow_id, "revision": shadow.revision}],
                person_id=pid,
                display_name=None,
                actor="independent-device",
            )
        if action == "confirm_reminder":
            intent = replace(
                _intent(scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1)),
                session_id=session_id,
                evidence_utterance_ids=(ids[0],),
                actor_person_id=pid,
            )
            submission = replace(
                _submission(intent), input_scope={"session_id": session_id}
            )
            candidate = f.core.reminders.submit_generation(submission)["candidates"][0]
            reminder = f.core.reminders.confirm(candidate["candidate_id"], "human")[
                "reminder"
            ]
            return reminder
        if action == "inspect":
            with SqliteUnitOfWork(f.core.database) as u:
                vectors = u.people.person_vectors(
                    provider.model, provider.model_version
                )
                facts = u.evidence.connection.execute(
                    "SELECT count(*) FROM annotation_facts"
                ).fetchone()[0]
            return {
                "matching_people": [p for p, _ in vectors],
                "fact_count": facts,
                "reminder": f.core.reminders.schedule(reminder["event_id"])
                if reminder
                else None,
            }
        raise ValueError(action)

    try:
        for line in sys.stdin:
            try:
                result = {"result": command(json.loads(line))}
            except Exception:
                result = {"error": traceback.format_exc()}
            print(json.dumps(result, ensure_ascii=True), flush=True)
    finally:
        try:
            next(generator)
        except StopIteration:
            pass
        f.core.close()
        f.tearDown()


if __name__ == "__main__":
    main()
