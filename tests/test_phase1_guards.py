"""Negative guards for task lifecycle and genuine downstream extraction."""

from datetime import timedelta
import pytest
from tests.test_phase1_human_facts import (
    people as people,
    reminders as reminders,
    assign,
    rerun,
)
from tests.test_v33_intelligent_reminders import _intent, _submission, UTTERANCE_ID
from tests.test_v34_open_speaker_identity import _utterance_id, _session_id
from allday_asr.v3.application import (
    CorrectionInvalidationService,
    CorrectUtteranceCommand,
)
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain import ReminderOperation


def correction(f, text):
    return CorrectionInvalidationService(
        f.factory, now=lambda: f.current_time
    ).correct_utterance(CorrectUtteranceCommand(UTTERANCE_ID, 1, text, "human"))


def test_substantive_change_invalidates_pending_candidate(reminders):
    f = reminders
    candidate = f.reminders.submit_generation(_submission(_intent()))["candidates"][0]
    correction(f, "The original promise is false.")
    after = f.reminders.candidate(candidate["candidate_id"])
    assert after["status"] == "conflict" and after["proposal_status"] == "rejected"
    assert after["conflict_reason"] == "source_semantics_changed"
    assert f.reminders.list_schedules() == ()


def test_confirmed_manual_edit_and_due_time_survive_source_contradiction(reminders):
    f = reminders
    candidate = f.reminders.submit_generation(_submission(_intent()))["candidates"][0]
    result = f.reminders.modify(
        candidate["candidate_id"],
        "human",
        {
            "title": "My edited task",
            "scheduled_at": (f.current_time + timedelta(hours=9)).isoformat(),
        },
    )["reminder"]
    correction(f, "The speaker denies the promise.")
    after = f.reminders.schedule(result["event_id"])
    assert after["source_review_required"] is True
    for key in ("title", "scheduled_at", "status", "event_revision"):
        assert after[key] == result[key]
    regenerated = f.reminders.submit_generation(
        _submission(
            _intent(
                title="Different automatic title",
                confidence=1,
                needs_confirmation=False,
            )
        )
    )["candidates"][0]
    assert regenerated["status"] == "pending_confirmation"
    assert len(f.reminders.list_schedules()) == 1


@pytest.mark.parametrize(
    "operation,status",
    [
        (ReminderOperation.CANCEL_EVENT, "cancelled"),
        (ReminderOperation.MARK_DONE, "completed"),
    ],
)
def test_user_terminal_lifecycle_is_not_resurrected(reminders, operation, status):
    f = reminders
    first = f._confirmed(_intent())["reminder"]
    candidate = f.reminders.submit_generation(
        _submission(
            _intent(
                operation=operation,
                target_event_id=first["event_id"],
                expected_revision=1,
                title=None,
                scheduled_at=None,
            )
        )
    )["candidates"][0]
    assert (
        f.reminders.confirm(candidate["candidate_id"], "human")["reminder"]["status"]
        == status
    )
    correction(f, "Recomputed text with a different possible task.")
    retry = f.reminders.submit_generation(
        _submission(
            _intent(
                title="Potential recreated task", confidence=1, needs_confirmation=False
            )
        )
    )["candidates"][0]
    assert retry["status"] == "pending_confirmation"
    assert f.reminders.schedule(first["event_id"])["status"] == status
    assert len(f.reminders.list_schedules()) == 1


@pytest.mark.parametrize("kind", ["non_speech", "media_speech"])
def test_restored_classification_controls_actual_extraction_and_interactions(
    people, kind
):
    f = people
    uid = _utterance_id(1)
    f.core.corrections.classify_segments([{"utterance_id": uid, "revision": 1}], kind)
    f._seed_reprocessed_track(1)
    with SqliteUnitOfWork(f.core.database) as uow:
        from dataclasses import replace

        template = uow.evidence.get_utterance(_utterance_id(1, reprocessed=True))
        uow.evidence.add_utterance(
            replace(template, utterance_id="00000000000000000000000999", ordinal=80)
        )
        uow.evidence.connection.execute(
            "UPDATE utterances SET status='stale' WHERE utterance_id!=?",
            ("00000000000000000000000999",),
        )
    for service in (f.core.semantic_events, f.core.reminder_extraction):
        with pytest.raises(ValueError, match="no active utterances"):
            service._request(_session_id(1))
    a = assign(f, "00000000000000000000000999", "Known")
    with SqliteUnitOfWork(f.core.database) as uow:
        assert uow.insights.person_interactions(a["person_id"]) == ()
        assert (
            uow.people.person_vectors(f.provider.model, f.provider.model_version) == ()
        )
        assert not uow.person_memories.person_detail(a["person_id"])["interactions"]


def test_old_identity_event_and_memory_dependencies_are_invalidated(people):
    from allday_asr.v3.domain.knowledge import DerivationDependency
    from tests.test_v34_open_speaker_identity import NOW

    f = people
    assign(f, _utterance_id(1), "A")
    restored = rerun(f)
    # Existing dependency mechanism is real; downstream resources needn't be
    # materialized to verify transitive closure and durable queued invalidations.
    with SqliteUnitOfWork(f.core.database) as uow:
        uow.derivations.add_dependency(
            DerivationDependency(
                "event", "identity-event", 1, "utterance", _utterance_id(1), 2, NOW
            )
        )
        uow.derivations.add_dependency(
            DerivationDependency(
                "memory", "identity-memory", 1, "event", "identity-event", 1, NOW
            )
        )
    assign(f, restored.utterance_id, "B")
    with SqliteUnitOfWork(f.core.database) as uow:
        assert uow.derivations.list_invalidations("event", "identity-event", 10)
        assert uow.derivations.list_invalidations("memory", "identity-memory", 10)
        assert {
            r["target_id"]
            for r in uow.derivations.list_recompute_requests("queued", 20)
        } >= {"identity-event", "identity-memory"}
        old = uow.evidence.get_utterance(_utterance_id(1))
        assert "person" in old.evidence["annotation_outdated"]


def test_existing_cluster_label_entry_revises_same_audio_fact(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    candidate = f.core.people.list_review_candidates(a["person_id"])[0]
    f.core.people.review_prototype(
        candidate["prototype_id"], a["person_id"], "confirmed"
    )
    b = f.core.people.create_person("B")
    f.core.people.label_cluster(a["cluster_id"], b["person_id"])
    restored = rerun(f)
    assert restored.evidence["person_annotation"]["person_id"] == b["person_id"]
    with SqliteUnitOfWork(f.core.database) as uow:
        assert not uow.people.person_vectors(f.provider.model, f.provider.model_version)
    f.core.people.undo(a["cluster_id"])
    assert rerun(f).evidence["person_annotation"]["person_id"] == a["person_id"]
