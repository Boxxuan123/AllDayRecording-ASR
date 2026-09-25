"""Cross-stage risks, using the existing human facts and reminder services."""

from dataclasses import replace
from unittest.mock import patch

from tests.test_phase1_human_facts import (
    people as people,
    reminders as reminders,
    assign,
    rerun,
)
from tests.test_v34_open_speaker_identity import FakeEmbeddingProvider, _utterance_id
from tests.test_v33_intelligent_reminders import _intent, UTTERANCE_ID
from allday_asr.v3.application import SpeakerIdentityService
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork


def test_cluster_correction_commits_with_confirmed_reminder(reminders):
    f = reminders
    provider = FakeEmbeddingProvider({})
    service = SpeakerIdentityService(f.factory, provider, f.knowledge)
    embed = provider.embed

    def fixed(tracks):
        for track in tracks:
            provider.vectors[track.speaker_track_id] = (1.0, 0.0, 0.0)
        return embed(tracks)

    with patch.object(provider, "embed", side_effect=fixed):
        a = service.assign_utterances(
            [{"utterance_id": UTTERANCE_ID, "revision": 1}],
            person_id=None,
            display_name="A",
            actor="human",
        )
        service.sample_worker.run_pending()
    candidate = service.list_review_candidates(a["person_id"])[0]
    service.review_prototype(candidate["prototype_id"], a["person_id"], "confirmed")
    before = f._confirmed(replace(_intent(), actor_person_id=a["person_id"]))[
        "reminder"
    ]
    b = service.create_person("B")
    result = service.label_cluster(a["cluster_id"], b["person_id"])
    assert result
    with f.factory() as uow:
        assert (
            uow.evidence.get_utterance(UTTERANCE_ID).evidence["person_annotation"][
                "person_id"
            ]
            == b["person_id"]
        )
        assert not uow.people.person_vectors(provider.model, provider.model_version)
    after = f.reminders.schedule(before["event_id"])
    assert {k: after[k] for k in ("status", "title", "scheduled_at")} == {
        k: before[k] for k in ("status", "title", "scheduled_at")
    }
    assert after["source_review_required"]


def test_restored_authorization_can_create_fresh_candidate(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    candidate = f.core.people.list_review_candidates(a["person_id"])[0]
    f.core.people.review_prototype(
        candidate["prototype_id"], a["person_id"], "confirmed"
    )
    b = f.core.people.create_person("B")
    f.core.people.label_cluster(a["cluster_id"], b["person_id"])
    f.core.people.undo(a["cluster_id"])
    assert rerun(f).evidence["person_annotation"]["person_id"] == a["person_id"]
    f.core.people.process_annotation_samples([{"utterance_id": _utterance_id(1)}])
    f.core.people.sample_worker.run_pending()
    fresh = f.core.people.list_review_candidates(a["person_id"])
    assert fresh and all(c["prototype_id"] != candidate["prototype_id"] for c in fresh)
    with SqliteUnitOfWork(f.core.database) as uow:
        assert (
            uow.evidence.connection.execute(
                "SELECT COUNT(*) FROM annotation_sample_revocations"
            ).fetchone()[0]
            > 0
        )
        assert not uow.people.person_vectors(f.provider.model, f.provider.model_version)
