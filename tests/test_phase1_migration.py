"""Upgrade/rollback on synthetic test databases only."""

import json
import pytest
from tests.test_phase1_human_facts import people as people, assign, rerun
from tests.test_v34_open_speaker_identity import _utterance_id
from allday_asr.v3.adapters.sqlite.annotation_fact_migration import (
    backfill_annotation_facts,
)


def v12_snapshot(f):
    with f.core.database.transaction() as db:
        for row in db.execute(
            "SELECT utterance_id,evidence_json FROM utterances"
        ).fetchall():
            evidence = json.loads(row["evidence_json"])
            evidence.pop("annotation_fact_ids", None)
            if "person_annotation" in evidence:
                evidence["person_annotation"].pop("fact_id", None)
            db.execute(
                "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
                (json.dumps(evidence), row["utterance_id"]),
            )
        for name in ("annotation_sample_utterance_insert", "annotation_sample_utterance_update", "annotation_sample_fact_change"):
            db.execute("DROP TRIGGER IF EXISTS " + name)
        for table in ("annotation_sample_sets", "annotation_sample_queue", "annotation_receipt_recovery", "annotation_sample_runtime"):
            db.execute("DROP TABLE IF EXISTS " + table)
        for table in (
            "annotation_sample_revocations",
            "annotation_supersessions",
            "annotation_fact_audio",
            "annotation_facts",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DELETE FROM schema_migrations WHERE version>=13")


def test_migration_preserves_explicit_correction_and_is_idempotent(people):
    f = people
    a = assign(f, _utterance_id(1), "A")
    restored = rerun(f)
    b = assign(f, restored.utterance_id, "B")
    rerun(f)
    f.core.corrections.classify_segments(
        [{"utterance_id": restored.utterance_id, "revision": 2}], "media_speech"
    )
    v12_snapshot(f)
    assert f.core.initialize() == 15
    final = rerun(f)
    assert final.evidence["person_annotation"]["person_id"] == b["person_id"]
    assert final.evidence["sound_kind"] == "media_speech"
    with f.core.database.transaction() as db:
        before = [
            tuple(r)
            for r in db.execute("SELECT * FROM annotation_facts ORDER BY fact_id")
        ]
        backfill_annotation_facts(db)
        after = [
            tuple(r)
            for r in db.execute("SELECT * FROM annotation_facts ORDER BY fact_id")
        ]
        assert after == before
        assert {
            json.loads(r["value_json"])
            for r in db.execute(
                "SELECT * FROM annotation_facts WHERE state='superseded'"
            )
        } == {a["person_id"]}
    assert f.core.initialize() == 15
    assert rerun(f).evidence["person_annotation"]["person_id"] == b["person_id"]
    assign(f, final.utterance_id, "C")
    assert rerun(f).evidence["person_annotation"]["person_id"] != b["person_id"]


def test_migration_keeps_unordered_conflict(people):
    f = people
    unrelated = rerun(f)
    assign(f, _utterance_id(1), "A")
    assign(f, unrelated.utterance_id, "B")
    v12_snapshot(f)
    f.core.initialize()
    assert "person" in rerun(f).evidence["annotation_review"]["dimensions"]


@pytest.mark.parametrize("kind", ["non_speech", "media_speech", "background_speech"])
def test_migration_of_standalone_sound_and_rollback(people, kind):
    f = people
    f.core.corrections.classify_segments(
        [{"utterance_id": _utterance_id(1), "revision": 1}], kind
    )
    v12_snapshot(f)
    from unittest.mock import patch

    with patch(
        "allday_asr.v3.adapters.sqlite.annotation_fact_migration.backfill_annotation_facts",
        side_effect=RuntimeError("migration failed"),
    ):
        with pytest.raises(RuntimeError):
            f.core.initialize()
    with f.core.database.transaction() as db:
        assert (
            db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 12
        )
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='annotation_facts'"
            ).fetchone()
            is None
        )
    f.core.initialize()
    assert rerun(f).evidence["sound_kind"] == kind


def test_missing_legacy_audio_mapping_is_preserved_for_review(people):
    f = people
    f.core.corrections.classify_segments(
        [{"utterance_id": _utterance_id(1), "revision": 1}], "non_speech"
    )
    v12_snapshot(f)
    # A historical session with no recoverable capture mapping cannot be
    # silently treated as ordinary speech after upgrade/rerun.
    with f.core.database.transaction() as db:
        db.execute("UPDATE utterances SET start_ms=20000,end_ms=25000")
    f.core.initialize()
    projected = rerun(f)
    assert "sound" in projected.evidence["annotation_review"]["dimensions"]
    from allday_asr.v3.domain.sound_kind import sound_uses

    assert not sound_uses(projected.evidence)["content_usable"]
