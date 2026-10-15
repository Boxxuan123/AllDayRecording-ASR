"""Security probes use a fourth synthetic fixture, never the blind questions."""

import sqlite3
import pytest
from tools.evaluate_isolated_agentic import seed, readonly, IsolatedSource, run_case
from allday_asr.v3.ports.chat_data import ChatDataError


@pytest.fixture
def isolated(tmp_path):
    root = tmp_path / "isolated"
    seed(
        {
            "id": "probe",
            "question": "security probe only",
            "timezone": "Asia/Singapore",
            "local_now": "2026-10-15T12:00:00+08:00",
            "contacts": [
                {"id": "synthetic-room", "name": "Synthetic room", "kind": "group"}
            ],
            "messages": [
                {
                    "id": 99001,
                    "text": "fictional isolated canary",
                    "sender": "synthetic-account",
                    "conversation": "synthetic-room",
                    "at": "2026-10-11T10:00:00+08:00",
                }
            ],
        },
        root,
    )
    return root


def test_sqlite_outside_and_write_refused(isolated, tmp_path):
    outside = tmp_path / "outside.sqlite3"
    with sqlite3.connect(outside) as conn:
        conn.execute("CREATE TABLE secret(value TEXT)")
    with pytest.raises(ValueError, match="outside"):
        readonly(isolated, outside)
    with readonly(isolated, isolated / "corpus.sqlite3") as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM records")
        assert conn.execute("SELECT count(*) FROM records").fetchone()[0] == 1


@pytest.mark.parametrize(
    "route",
    [
        "/v1/snapshots",
        "/v1/changes",
        "file:///outside.sqlite3",
        "https://production.example/v1/messages",
    ],
)
def test_routes_denied_before_source_access(isolated, route):
    source = IsolatedSource(isolated)
    with pytest.raises(ChatDataError):
        source.request(route)
    assert source.calls == [] and source.fixture.calls == []


def test_cross_case_record_denied_and_only_seeded_data(isolated):
    source = IsolatedSource(isolated)
    with pytest.raises(ChatDataError):
        source.request("/v1/context", {"record_id": "rec:other-case"})
    result = source.request("/v1/messages", scope={"platform": "qq"})
    assert [r["record_id"] for r in result["items"]] == ["rec:fixture-99001"]
    assert (
        source.request("/v1/messages", scope={"source_account_id": "production"})[
            "items"
        ]
        == []
    )
    with sqlite3.connect(isolated / "core.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM persons").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM person_profile_revisions").fetchone()[0]
            == 0
        )


def test_first_execution_lock_blocks_reentry_before_question(isolated):
    (isolated / "FIRST_EXECUTION.lock").write_text("already reserved", "utf-8")
    with pytest.raises(FileExistsError):
        run_case(isolated)
    assert not (isolated / "model-attempts.json").exists()
