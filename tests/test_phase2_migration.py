"""v13 -> v14 on synthetic existing data, with failure and repeat validation."""

import sqlite3
from dataclasses import replace
import pytest

from tests.test_phase1_human_facts import people as people, assign
from tests.test_v34_open_speaker_identity import _utterance_id
from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner


def test_existing_annotations_backfill_atomically_and_idempotently(people):
    f = people
    person = assign(f, _utterance_id(1), "Synthetic A")
    candidate = f.core.people.list_review_candidates(person["person_id"])[0]
    f.core.people.review_prototype(
        candidate["prototype_id"], person["person_id"], "confirmed"
    )
    with f.core.database.transaction() as db:
        facts = [
            tuple(r)
            for r in db.execute("SELECT * FROM annotation_facts ORDER BY fact_id")
        ]
        prototypes = [
            tuple(r)
            for r in db.execute(
                "SELECT prototype_id,status,model,model_version FROM voice_prototypes ORDER BY prototype_id"
            )
        ]
        for name in (
            "annotation_sample_utterance_insert",
            "annotation_sample_utterance_update",
            "annotation_sample_fact_change",
        ):
            db.execute("DROP TRIGGER " + name)
        for table in (
            "annotation_sample_sets",
            "annotation_sample_queue",
            "annotation_receipt_recovery", "annotation_sample_runtime",
        ):
            db.execute("DROP TABLE " + table)
        db.execute("DELETE FROM schema_migrations WHERE version>=14")
    broken = replace(
        MIGRATIONS[13], sql=MIGRATIONS[13].sql + "\nSELECT missing_migration_column;\n"
    )
    with pytest.raises(sqlite3.OperationalError):
        V3MigrationRunner(
            f.core.paths.database_path, migrations=(*MIGRATIONS[:13], broken)
        ).initialize()
    with f.core.database.transaction() as db:
        assert (
            db.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 13
        )
        assert (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='annotation_sample_queue'"
            ).fetchone()
            is None
        )
    assert f.core.initialize() == 15
    assert f.core.initialize() == 15
    with f.core.database.transaction() as db:
        assert [
            tuple(r)
            for r in db.execute("SELECT * FROM annotation_facts ORDER BY fact_id")
        ] == facts
        assert [
            tuple(r)
            for r in db.execute(
                "SELECT prototype_id,status,model,model_version FROM voice_prototypes ORDER BY prototype_id"
            )
        ] == prototypes
        assert (
            db.execute("SELECT count(*) FROM annotation_sample_queue").fetchone()[0]
            == 1
        )
    f.core.people.sample_worker.run_pending()
    with f.core.database.transaction() as db:
        count = db.execute("SELECT count(*) FROM voice_prototypes").fetchone()[0]
    f.core.initialize()
    f.core.people.process_annotation_samples([{"utterance_id": _utterance_id(1)}])
    f.core.people.sample_worker.run_pending()
    with f.core.database.transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM voice_prototypes").fetchone()[0] == count
        )
        assert [
            tuple(r)
            for r in db.execute("SELECT * FROM annotation_facts ORDER BY fact_id")
        ] == facts
