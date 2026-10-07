"""The purity ledger is append-only and independent of person facts."""

import json
import sqlite3

import pytest

from allday_asr.v3.adapters.sqlite.migrations.v021_speaker_profile_purity import SQL
from allday_asr.v3.adapters.sqlite.speaker_profile_purity import (
    append_review,
    latest_reviews,
    list_phone_tasks,
    source_key,
)


def audit_db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE persons(person_id TEXT PRIMARY KEY, display_name TEXT)")
    connection.executemany(
        "INSERT INTO persons VALUES(?,?)", (("zhang", "Z"), ("self", "S"))
    )
    connection.executescript(SQL)
    connection.execute(
        "INSERT INTO speaker_profile_purity_runs VALUES(?,?,?,?,?,?,?,?)",
        ("run", "v1", "CAM++", "v1", "embedding", "profile", "{}", "2026-09-28T00:00:00Z"),
    )
    connection.execute(
        "INSERT INTO speaker_profile_purity_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("task", "run", source_key("media", 1000, 4000), "zhang", "media", 1000, 4000,
         "session", "track", "cluster", "P0", '["active_profile_source"]',
         json.dumps({"prototype_ids": ["prototype"]}), "2026-09-28T00:00:00Z"),
    )
    return connection


def test_purity_requires_separate_identity_and_clean_verdict():
    connection = audit_db()
    result = append_review(connection, "task", {
        "action": "submit", "primary_speaker_person_id": "zhang",
        "primary_speaker_unknown": False, "purity": "mixed_overlap",
        "other_speaker_ids": ["self"], "quality_flags": [],
    }, "device")
    assert result["revision"] == 1
    assert latest_reviews(connection)["task"]["purity"] == "mixed_overlap"
    assert not list_phone_tasks(connection)
    history = list_phone_tasks(connection, history=True)
    assert len(history) == 1
    assert history[0]["context"]["purity_review"]["other_speaker_ids"] == ["self"]
    # Correction appends; the original mixed judgement remains queryable.
    append_review(connection, "task", {
        "action": "submit", "primary_speaker_person_id": "self",
        "primary_speaker_unknown": False, "purity": "clean_single",
        "other_speaker_ids": [], "quality_flags": [],
    }, "device")
    assert connection.execute("SELECT COUNT(*) FROM speaker_profile_purity_reviews").fetchone()[0] == 2
    assert latest_reviews(connection)["task"]["primary_speaker_person_id"] == "self"
    append_review(connection, "task", {"action": "undo"}, "device")
    assert len(list_phone_tasks(connection)) == 1
    assert latest_reviews(connection)["task"]["revision"] == 3
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        connection.execute("UPDATE speaker_profile_purity_reviews SET purity='uncertain'")


def test_duplicate_source_range_and_invalid_verdict_rejected():
    connection = audit_db()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO speaker_profile_purity_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("duplicate", "run", source_key("media", 1000, 4000), "self", "media", 1000,
             4000, "session", "track", "cluster", "P1", "[]", "{}", "2026-09-28T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="purity verdict"):
        append_review(connection, "task", {
            "action": "submit", "primary_speaker_person_id": "zhang",
            "primary_speaker_unknown": False, "purity": "accepted",
        }, "device")


def test_phone_retries_same_purity_operation_without_duplicate_revision():
    connection = audit_db()
    request = {
        "operation_id": "01TESTPURITIDEMPOTENT00000001",
        "action": "submit", "primary_speaker_person_id": "zhang",
        "primary_speaker_unknown": False, "purity": "clean_single",
        "other_speaker_ids": [], "quality_flags": [],
    }
    first = append_review(connection, "task", request, "device")
    assert append_review(connection, "task", request, "device") == first
    assert connection.execute("SELECT COUNT(*) FROM speaker_profile_purity_reviews").fetchone()[0] == 1
    with pytest.raises(ValueError, match="different answer"):
        append_review(connection, "task", {**request, "purity": "uncertain"}, "device")
