"""Fixed semantic synthetic load, no private data or real model."""

import json
from dataclasses import replace
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain.ids import stable_ulid
from tests.test_v34_open_speaker_identity import _utterance_id


def seed(f, count):
    f._seed_track(2, duration_ms=count * 1000)
    person = f.core.people.create_person("Synthetic Person")["person_id"]
    with SqliteUnitOfWork(f.core.database) as u:
        old = u.evidence.get_utterance(_utterance_id(2))
        db = u.people.connection
        db.execute("DELETE FROM utterances")
        ids = []
        for i in range(count):
            uid = stable_ulid("latency-row", str(i))
            ids.append(uid)
            row = replace(
                old,
                utterance_id=uid,
                ordinal=i,
                start_ms=i * 1000,
                end_ms=(i + 1) * 1000,
                revision=1,
                evidence={},
            )
            u.evidence.add_utterance(row)
            refs = [dict(media_id="media-2", start_ms=i * 1000, end_ms=(i + 1) * 1000)]
            pf, sf = (
                stable_ulid("latency-person", str(i)),
                stable_ulid("latency-sound", str(i)),
            )
            u.evidence.insert_fact(
                pf,
                "person",
                person,
                row,
                refs,
                {"speaker_track_id": old.speaker_track_id},
                "synthetic",
                "2026-09-27T00:00:00Z",
            )
            u.evidence.insert_fact(
                sf,
                "sound",
                "live_speech",
                row,
                refs,
                {},
                "synthetic",
                "2026-09-27T00:00:00Z",
            )
            evidence = {
                "sound_kind": "live_speech",
                "annotation_fact_ids": {"person": [pf], "sound": [sf]},
            }
            db.execute(
                "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
                (json.dumps(evidence), uid),
            )
        u.people.enqueue_samples(old.session_id)
    return old.session_id, person, ids
