"""Prospective allocation, actual product/review and learning isolation."""

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tests.test_blind_validation import world as world
from tests.test_self_identity_regression import Provider, anchor, evidence
from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3MigrationRunner
from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
from allday_asr.v3.adapters.sqlite.speaker_research_reservations import (
    provenance,
    record_use,
)


def prospective(world, role="independent_evaluation", number=1, same_day=None):
    helper, core, *_ = world
    with core.database.read() as c:
        stamp = c.execute(
            "SELECT activated_at FROM speaker_research_policy"
        ).fetchone()[0]
    day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).date() + timedelta(
        days=1
    )
    wanted = {"independent_evaluation": 0, "development": 4, "learning": 1}[role]
    while (day - datetime(1970, 1, 1).date()).days % 5 != wanted:
        day += timedelta(days=1)
    moment = same_day or datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    text = moment.isoformat()
    with (
        patch.object(fixtures, "NOW", moment),
        patch.object(fixtures, "NOW_TEXT", text),
        patch.object(
            fixtures, "UTTERANCE_END_TEXT", (moment + timedelta(seconds=10)).isoformat()
        ),
    ):
        helper._seed_track(number)
    return fixtures._session_id(number), moment


def learning_state(core):
    with core.database.read() as c:
        return {
            t: [tuple(r) for r in c.execute("SELECT * FROM " + t)]
            for t in (
                "voice_prototypes",
                "person_profile_revisions",
                "person_identity_policy_revisions",
                "purity_candidates",
                "session_learning_exposure",
                "annotation_sample_sets",
                "annotation_sample_queue",
            )
        }


def test_reservation_precedes_actual_product_prediction_and_review_without_learning(
    world,
):
    _, core, *_ = world
    session, _ = prospective(world)
    evidence(core, session)
    anchor(core)

    class Observe(Provider):
        def embed(self, tracks):
            with core.database.read() as c:
                r = provenance(c, fixtures._utterance_id(1))
                assert r["is_independent_evaluation"]
                reserved = r["reservation"]
                assert reserved["reserved_at"] < reserved["first_prediction_at"]
            return super().embed(tracks)

    core.people._provider = Observe()
    before = learning_state(core)
    result = core.people.analyze(session)
    assert (
        result["product_inference_executed"]
        and result["product_self_matched_utterance_count"] == 1
    )
    assert result["research_reservation"]["research_role"] == "independent_evaluation"
    assert learning_state(core) == before
    with core.database.transaction() as c:
        assert (
            c.execute(
                "SELECT dataset_role FROM session_dataset_roles WHERE session_id=?",
                (session,),
            ).fetchone()[0]
            == "holdout"
        )
        for purpose in (
            "enrollment",
            "calibration",
            "profile_learning",
            "threshold_fitting",
            "rule_fitting",
            "model_selection",
            "candidate_design",
            "training",
            "development",
            "diagnostic",
            "blind",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="independent evaluation"):
                record_use(c, session, purpose, "forbidden")
        with pytest.raises(sqlite3.IntegrityError, match="learning exposure"):
            c.execute(
                "INSERT INTO session_learning_exposure VALUES(?,'training','forbidden','now')",
                (session,),
            )
    core.people.assign_utterances(
        [{"utterance_id": fixtures._utterance_id(1), "revision": 2}],
        person_id=world[4],
        display_name=None,
        actor="test-human-review",
    )
    assert learning_state(core) == before
    with SqliteUnitOfWork(core.database) as uow:
        uow.people.enqueue_samples(session)
        assert uow.people.sample_plans(session, "fixture-speaker", "1")[0] == ()
        with pytest.raises(ValueError, match="dataset_role_excluded"):
            uow.people.confirmed_enrollment_input(session, "bad", ((0, 8000),))
        assert uow.people.speaker_event_provenance(fixtures._utterance_id(1))[
            "is_independent_evaluation"
        ]
    assert learning_state(core) == before


def test_same_date_reservation_is_stable_across_ingest_and_prior_blind_mode(world):
    _, core, *_ = world
    with core.database.transaction() as c:
        c.execute("UPDATE dataset_reservation_settings SET blind_collection_mode=1")
    one, moment = prospective(world, number=1)
    two, _ = prospective(world, number=2, same_day=moment + timedelta(hours=2))
    with core.database.read() as c:
        rows = [
            dict(r) for r in c.execute("SELECT * FROM session_speaker_reservations")
        ]
        assert {r["session_id"] for r in rows} == {one, two}
        assert {r["research_role"] for r in rows} == {"independent_evaluation"}
        assert len({r["capture_date_utc"] for r in rows}) == 1
        assert c.execute("SELECT COUNT(*) FROM blind_shadow_jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE session_speaker_reservations SET research_role='learning'",
        "DELETE FROM session_speaker_reservations",
        "UPDATE session_speaker_reservations SET first_prediction_at=reserved_at",
        "UPDATE recording_sessions SET captured_start='2099-01-01T00:00:00Z'",
        "UPDATE session_dataset_roles SET dataset_role='learning',role_revision=role_revision+1",
    ],
)
def test_independent_role_and_date_cannot_be_repurposed(world, mutation):
    _, core, *_ = world
    prospective(world)
    with core.database.transaction() as c, pytest.raises(sqlite3.IntegrityError):
        c.execute(mutation)


def test_migration_keeps_all_historical_table_rows_and_never_retags_old_sessions(
    tmp_path,
):
    path = tmp_path / "core.sqlite3"
    V3MigrationRunner(path, migrations=MIGRATIONS[:24]).initialize()
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO recording_sessions(session_id,captured_start,timezone,state,revision,status_code,progress,created_at,updated_at) VALUES('historical','2099-01-01T00:00:00Z','UTC','sealed',1,'available',1,'old','old')"
    )
    c.commit()
    tables = [
        r[0]
        for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'"
        )
    ]
    before = {t: list(c.execute("SELECT * FROM " + t)) for t in tables}
    c.close()
    V3MigrationRunner(path).initialize()
    c = sqlite3.connect(path)
    assert {t: list(c.execute("SELECT * FROM " + t)) for t in tables} == before
    assert (
        c.execute("SELECT COUNT(*) FROM session_speaker_reservations").fetchone()[0]
        == 0
    )
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
