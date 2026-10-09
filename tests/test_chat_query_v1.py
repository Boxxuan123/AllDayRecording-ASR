import copy
import json
import sqlite3
import time
from pathlib import Path
from threading import Event

import pytest

from tests.chat_fixture import FixtureHTTP, FixtureMac, NOW, RECORDS, TOKEN, record
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.application.chat_queries import checked_scope
from allday_asr.v3.bootstrap.chat import compose_chat_queries as ChatQueries
from allday_asr.v3.ports.chat_data import ChatDataError


@pytest.fixture
def service(tmp_path):
    obj = ChatQueries(tmp_path, FixtureMac(), start_worker=False)
    yield obj
    obj.close()


CASES = json.loads(
    (Path(__file__).parent / "fixtures/chat_query_v1/questions.json").read_text(
        encoding="utf-8"
    )
)


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["id"]))
def test_fixed_twenty_fictional_questions(service, case):
    page = service.search(case["scope"], limit=100)
    found = {r["record_id"] for r in page["items"]}
    assert set(case["expected_evidence_ids"]) <= found
    if not case["expected_evidence_ids"]:
        assert not found


@pytest.mark.parametrize(
    "keyword",
    [
        "杭电",
        "报名",
        "连续中文短句",
        "API",
        "12345",
        "report_v2.PDF",
        '20%_ "quoted"',
        "' OR 1=1 --",
    ],
)
def test_chinese_mixed_literal_local_search(service, keyword):
    page = service.search({"sent_from": 0, "keyword": keyword}, limit=100)
    service.cache.process()
    local = service.search(
        {"sent_from": 0, "keyword": keyword}, origin="cache", limit=100
    )
    assert {r["record_id"] for r in page["items"]} == {
        r["record_id"] for r in local["items"]
    }
    assert local["items"]


def test_all_filters_combine_without_nickname_merge(service):
    scope = {
        "platform": "qq",
        "source_account_id": "source-qq",
        "conversation_id": "group",
        "sender_account_id": "alice",
        "sent_from": NOW,
        "sent_to": NOW + 10,
        "keyword": "会议",
    }
    assert [r["record_id"] for r in service.search(scope)["items"]] == ["rec:fixture-1"]


def test_http_adapter_auth_contract_paging_and_partial_records(tmp_path):
    remote = FixtureHTTP(FixtureMac())
    try:
        with pytest.raises(ChatDataError, match="UNAUTHORIZED"):
            MacChatData(remote.url, "wrong").request("/v1/status")
        client = MacChatData(remote.url, TOKEN)
        app = ChatQueries(tmp_path, client, start_worker=False)
        assert app.status()["error"] is None
        page = app.search({"sent_from": 0}, limit=2)
        second = app.search({"sent_from": 0}, cursor=page["next_cursor"], limit=2)
        assert not {r["record_id"] for r in page["items"]} & {
            r["record_id"] for r in second["items"]
        }
        batch = client.request(
            "/v1/records",
            {
                "items": [
                    {"record_id": "rec:fixture-1", "record_revision": 1},
                    {"record_id": "rec:missing", "record_revision": 1},
                    {"record_id": "rec:fixture-1", "record_revision": 2},
                ]
            },
        )
        assert [r["status"] for r in batch["items"]] == [
            "found",
            "not_found",
            "version_unavailable",
        ]
        with pytest.raises(ChatDataError, match="SCOPE_MISMATCH"):
            app.search(
                {"platform": "wechat", "sent_from": 0}, cursor=page["next_cursor"]
            )
        app.close()
    finally:
        remote.close()


def test_snapshot_late_arrival_restart_and_replay(tmp_path):
    remote = FixtureMac([record(i, "报名") for i in range(1, 251)])
    app = ChatQueries(tmp_path, remote, start_worker=False)
    scope = {
        "platform": "qq",
        "source_account_id": "source-qq",
        "conversation_id": "group",
        "sent_from": 0,
    }
    app.start_sync([scope])
    app.sync_once(max_pages=1)
    # Snapshot boundary was 250, not max sent_at. An OLD record captured during pagination enters changes.
    remote.records.append(record(251, "迟到旧历史", sent=1))
    app.close()
    app = ChatQueries(tmp_path, remote, start_worker=False)
    app.controls("resume")
    app.sync_once()
    assert app.cache.record("rec:fixture-251")["sent_at"] == 1
    assert app.cache.health()["received"] == 251
    row = app.cache.scopes()[0]
    before = app.cache.health().copy()
    cursor = row["receive_cursor"]
    app.sync_once()
    app.sync_once()
    assert app.cache.health() == before
    assert app.cache.scopes()[0]["receive_cursor"] == cursor
    app.close()


def test_transaction_rolls_back_body_tasks_and_cursor(service):
    scope = {
        "platform": "qq",
        "source_account_id": "source-qq",
        "conversation_id": "group",
        "sent_from": 0,
    }
    service.start_sync([scope])
    row = service.cache.scopes()[0]
    invalid = record(500, "bad")
    invalid["record_revision"] = 0
    with pytest.raises(ChatDataError, match="INVALID_CHAT_RECORD"):
        service.cache.receive(
            row["dataset"],
            [record(499, "good"), invalid],
            scope_id=row["id"],
            progress={
                "page_cursor": "new",
                "receive_cursor": "new",
                "phase": "changes",
            },
        )
    assert service.cache.health()["received"] == 0
    assert service.cache.health()["pending"] == 0
    assert service.cache.scope(row["id"])["receive_cursor"] is None
    assert service.cache.scope(row["id"])["page_cursor"] is None


def test_duplicate_id_revision_task_is_idempotent(service):
    service.discover()
    service.cache.receive(service.source.dataset, [RECORDS[0], RECORDS[0]])
    service.cache.receive(service.source.dataset, [RECORDS[0]])
    assert service.cache.health()["received"] == 1
    assert service.cache.health()["pending"] == 1


def test_failed_gap_is_not_crossed_and_retry_recovers(service):
    service.search({"sent_from": 0}, limit=100)

    def fail(task):
        if task["id"] == 2:
            raise RuntimeError("synthetic crash")

    service.cache.process(fail=fail)
    assert service.cache.health()["failed"] == 1
    assert service.cache.health()["processing_watermark"] == 1
    assert service.cache.health()["indexed"] == 19
    service.cache.retry_index()
    service.cache.process()
    assert service.cache.health()["failed"] == 0
    assert service.cache.health()["processing_watermark"] == 20


def test_source_revision_update_delete_preserves_user_state(service):
    service.search({"sent_from": 0}, limit=100)
    service.cache.process()
    with service.cache.connect() as db:
        db.execute(
            "INSERT INTO user_account_links VALUES(?,?,?,?)",
            ("qq", "source-qq", "alice", "confirmed-person"),
        )
    old_generation = service.cache.get("generation")
    changed = copy.deepcopy(RECORDS[0])
    changed["record_revision"] = 2
    changed["text"] = "修订后的新原话"
    event = {
        "operation": "update",
        "record_id": changed["record_id"],
        "record_revision": 2,
        "record": changed,
    }
    service.cache.receive(service.source.dataset, [event], changes=True)
    assert (
        not service.cache.search({"keyword": "会议", "conversation_id": "group"})[
            "items"
        ][0]["record_id"]
        == "rec:fixture-1"
    )
    service.cache.process()
    assert (
        service.cache.search({"keyword": "修订后的新原话"})["items"][0][
            "record_revision"
        ]
        == 2
    )
    deleted = {
        "operation": "delete",
        "record_id": changed["record_id"],
        "record_revision": 3,
    }
    service.cache.receive(service.source.dataset, [deleted], changes=True)
    service.cache.process()
    assert service.cache.record(changed["record_id"]) is None
    assert service.cache.get("generation") > old_generation
    with service.cache.connect() as db:
        assert (
            db.execute("SELECT person_id FROM user_account_links").fetchone()[0]
            == "confirmed-person"
        )


def test_immutable_revision_mismatch_never_overwrites(service):
    service.search({"sent_from": 0, "keyword": "杭电"})
    changed = copy.deepcopy(RECORDS[0])
    changed["text"] = "incorrect current body for old revision"
    with pytest.raises(ChatDataError, match="IMMUTABLE_REVISION_CHANGED"):
        service.cache.receive(service.source.dataset, [changed])
    assert service.cache.record(changed["record_id"])["text"] == RECORDS[0]["text"]


def test_context_never_mixes_conversation_or_sender(service):
    context = service.context("rec:fixture-1", 25, 25)
    assert {r["conversation_id"] for r in context["items"]} == {"group"}
    assert {r["sender_account_id"] for r in context["items"]} == {"alice", "bob"}
    assert "会议取消" in "".join(r["text"] or "" for r in context["items"])


def test_offline_cache_and_errors_are_not_empty_success(service):
    service.search({"sent_from": 0})
    service.cache.process()
    service.context("rec:fixture-1")
    service.source.failure = "MAC_OFFLINE"
    service._last_discovery = 0
    result = service.search({"sent_from": 0, "keyword": "报名"})
    assert result["origin"] == "cache" and not result["complete"] and result["items"]
    assert service.status()["error"] == "MAC_OFFLINE"
    assert service.context("rec:fixture-1")["origin"] == "cache"
    service.source.failure = "UNAUTHORIZED"
    with pytest.raises(ChatDataError, match="UNAUTHORIZED"):
        service.search({"sent_from": 0})


def test_dataset_change_blocks_old_sync_without_auto_history(service):
    service.start_sync(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
                "sent_from": 0,
            }
        ]
    )
    service.source.dataset = "replacement"
    service._last_discovery = 0
    service.sync_once()
    assert service.cache.scopes()[0]["error"] == "DATASET_MISMATCH"
    assert not any(route == "/v1/messages" for route, _, _ in service.source.calls)


def test_workset_budget_stops_at_5000(tmp_path):
    remote = FixtureMac([record(i, "预算消息") for i in range(1, 5002)])
    app = ChatQueries(tmp_path, remote, start_worker=False)
    app.start_sync(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
                "sent_from": 0,
            }
        ]
    )
    app.sync_once(max_pages=60, seconds=30)
    assert app.cache.health()["received"] == 5000
    assert app.cache.scopes()[0]["error"] == "WORKSET_BUDGET_REACHED"
    assert app._paused
    app.close()


def test_retries_are_bounded_and_scope_error_visible(service):
    service.start_sync(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
                "sent_from": 0,
            }
        ]
    )
    before = len(service.source.calls)
    service.source.failure = "QUERY_TIMEOUT"
    service.sync_once(max_pages=50)
    assert len(service.source.calls) - before == 3
    assert service.cache.scopes()[0]["error"] == "QUERY_TIMEOUT"


def test_answer_includes_changes_even_without_original_keyword(service):
    def model(payload, cancel):
        texts = [r["text"] or "" for r in payload["records"]]
        assert any("会议取消" in text for text in texts)
        assert "source_limitations" in payload
        return {
            "claims": [
                {
                    "text": "会议取消，暂不举办",
                    "kind": "quote",
                    "citations": ["rec:fixture-3"],
                }
            ],
            "unknown": [],
        }

    service.model = model
    result = service.answer(
        "最后的安排是什么", {"sent_from": 0, "keyword": "提议"}, Event()
    )
    assert result["claims"][0]["citations"] == ["rec:fixture-3"]
    assert "rec:fixture-2" in {r["record_id"] for r in result["evidence"]}
    assert result["scope"]["keyword"] == "提议"


def test_answer_no_records_requires_no_model_call(service):
    service.model = lambda *_: (_ for _ in ()).throw(AssertionError("should not run"))
    result = service.answer(
        "没有证据的问题", {"sent_from": 0, "keyword": "完全没有命中"}, Event()
    )
    assert not result["claims"] and result["unknown"]


def test_answer_citation_and_quote_validation(service):
    service.model = lambda *_: {
        "claims": [{"text": "fake", "kind": "summary", "citations": ["invented-id"]}],
        "unknown": [],
    }
    with pytest.raises(ChatDataError, match="INVALID_MODEL_CITATION"):
        service.answer("问题", {"sent_from": 0, "keyword": "杭电"}, Event())
    service.model = lambda *_: {
        "claims": [
            {"text": "made-up quote", "kind": "quote", "citations": ["rec:fixture-1"]}
        ],
        "unknown": [],
    }
    with pytest.raises(ChatDataError, match="INVALID_MODEL_QUOTE"):
        service.answer("问题", {"sent_from": 0, "keyword": "杭电"}, Event())


def test_source_mutation_during_answer_invalidates_publication(service):
    def model(*_):
        service.cache.receive(service.source.dataset, [record(999, "新消息")])
        return {
            "claims": [
                {
                    "text": "杭电 报名 会议提议周五 10:00",
                    "kind": "quote",
                    "citations": ["rec:fixture-1"],
                }
            ],
            "unknown": [],
        }

    service.model = model
    with pytest.raises(ChatDataError, match="ANSWER_SOURCE_CHANGED"):
        service.answer("问题", {"sent_from": 0, "keyword": "杭电"}, Event())


def test_answer_job_timeout_and_cancel_are_explicit(service):
    def timeout(*_):
        raise TimeoutError("synthetic model timeout")

    service.model = timeout
    job = service.submit_answer("问题", {"sent_from": 0, "keyword": "杭电"})
    until = time.monotonic() + 3
    while (
        service.cache.job(job["id"])["state"] in {"pending", "running"}
        and time.monotonic() < until
    ):
        time.sleep(0.01)
    assert service.cache.job(job["id"])["state"] == "failed"
    assert service.cache.job(job["id"])["error"] == "MODEL_TIMEOUT"
    cancelled = Event()
    cancelled.set()
    with pytest.raises(ChatDataError, match="CANCELLED"):
        service.answer("问题", {"sent_from": 0}, cancelled)


def test_rebuildable_index_does_not_remove_user_links(service):
    service.search({"sent_from": 0})
    service.cache.process()
    with service.cache.connect() as db:
        db.execute(
            "INSERT INTO user_account_links VALUES(?,?,?,?)",
            ("qq", "source-qq", "alice", "person"),
        )
        db.execute("DELETE FROM search_index")
        db.execute("UPDATE tasks SET state='pending'")
    service.cache.process()
    with service.cache.connect() as db:
        assert db.execute("SELECT count(*) FROM user_account_links").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM search_index").fetchone()[0] == 20


@pytest.mark.parametrize(
    "scope",
    [
        {"unknown": "x"},
        {"platform": "not-qq"},
        {"sent_from": True},
        {"sent_from": 10, "sent_to": 1},
        {"keyword": "x" * 201},
    ],
)
def test_invalid_scope(scope):
    with pytest.raises(ChatDataError):
        checked_scope(scope)


def test_cache_schema_refuses_unknown_version(tmp_path):
    path = tmp_path / "cache.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ChatDataError, match="CHAT_CACHE_SCHEMA_UNSUPPORTED"):
        ChatCache(path)


def test_incremental_receipt_continues_after_initial_5000(tmp_path):
    remote = FixtureMac([record(i, "消息") for i in range(1, 5001)])
    app = ChatQueries(tmp_path, remote, start_worker=False)
    app.start_sync(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
                "sent_from": 0,
            }
        ]
    )
    app.sync_once(max_pages=60, seconds=30)
    assert app.cache.scopes()[0]["phase"] == "changes"
    remote.records.append(record(5001, "新消息"))
    app.sync_once()
    assert app.cache.record("rec:fixture-5001")["text"] == "新消息"
    assert not app._paused
    app.close()


def test_real_process_interruption_during_transaction_and_after_commit(tmp_path):
    import os
    import subprocess
    import sys

    remote = FixtureMac([record(1, "受控重放")])
    app = ChatQueries(tmp_path, remote, start_worker=False)
    app.start_sync(
        [
            {
                "platform": "qq",
                "source_account_id": "source-qq",
                "conversation_id": "group",
                "sent_from": 0,
            }
        ]
    )
    row = app.cache.scopes()[0]
    marker = tmp_path / "transaction-open"
    script = (
        "import sqlite3,time; from pathlib import Path; db=sqlite3.connect("
        + repr(str(app.cache.path))
        + "); db.execute('BEGIN IMMEDIATE'); db.execute(\"UPDATE scopes SET receive_cursor='UNCOMMITTED'\"); Path("
        + repr(str(marker))
        + ").write_text('ready'); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ))
    try:
        until = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.02)
        assert marker.exists()
    finally:
        child.terminate()  # Only this test-owned process; no PID enumeration.
        child.wait(timeout=5)
    assert app.cache.scope(row["id"])["receive_cursor"] is None
    item = remote.records[0]
    script = (
        "from pathlib import Path; from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache; import os; cache=ChatCache(Path("
        + repr(str(app.cache.path))
        + ")); cache.receive("
        + repr(remote.dataset)
        + ",["
        + repr(item)
        + "],scope_id="
        + repr(row["id"])
        + ",progress={'page_cursor':'P','receive_cursor':'C','phase':'changes'}); os._exit(7)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], env=dict(os.environ))
    assert child.wait(timeout=5) == 7
    assert app.cache.scope(row["id"])["receive_cursor"] == "C"
    assert app.cache.health()["pending"] == 1
    app.cache.process()
    assert (
        app.cache.search({"keyword": "受控重放"})["items"][0]["record_id"]
        == item["record_id"]
    )
    app.close()


def test_authenticated_desktop_routes_and_chat_deep_link(tmp_path):
    import threading
    from urllib.error import HTTPError
    from urllib.parse import urlencode
    from urllib.request import Request, urlopen
    from allday_asr.v3.bootstrap import V3CorePaths
    from allday_asr.v3.interfaces.desktop_server import create_v3_desktop_server

    server = create_v3_desktop_server(
        paths=V3CorePaths.from_state_dir(tmp_path / "desktop"),
        port=0,
        token="desktop-test-token",
    )
    server.application.chat.close()
    app = ChatQueries(tmp_path / "chat", FixtureMac(), start_worker=False)
    server.application.chat = app
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = server.application.base_url
    headers = {
        "X-AllDay-Desktop-Session": "desktop-test-token",
        "Origin": base,
        "Content-Type": "application/json",
    }
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/v3/chat/status", timeout=5)
        assert error.value.code == 403
        query = urlencode({"scope": json.dumps({"sent_from": 0, "keyword": "杭电"})})
        with urlopen(
            Request(base + "/api/v3/chat/messages?" + query, headers=headers), timeout=5
        ) as response:
            result = json.load(response)
        assert result["items"][0]["record_id"] == "rec:fixture-1"
        with urlopen(Request(base + "/chat", headers=headers), timeout=5) as response:
            assert b"AllDay Recording V3" in response.read()
        body = json.dumps(
            {
                "scopes": [
                    {
                        "platform": "qq",
                        "source_account_id": "source-qq",
                        "conversation_id": "group",
                        "sent_from": 0,
                    }
                ]
            }
        ).encode()
        with urlopen(
            Request(base + "/api/v3/chat/sync", data=body, headers=headers), timeout=5
        ) as response:
            assert json.load(response)["scopes"]
        with pytest.raises(HTTPError) as error:
            urlopen(
                Request(
                    base + "/api/v3/chat/control",
                    data=b'{"action":"pause"}',
                    headers={**headers, "Origin": "http://untrusted.example"},
                ),
                timeout=5,
            )
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_private_connection_configuration_never_returns_token(tmp_path):
    path = tmp_path / "connection.json"
    client = MacChatData(config_path=path)
    with pytest.raises(ChatDataError, match="INVALID_CHAT_BASE_URL"):
        client.configure("http://user:password@example.com", TOKEN)
    response = client.configure("http://127.0.0.1:8091", TOKEN)
    assert "token" not in response and TOKEN not in json.dumps(response)
    assert json.loads(path.read_text())["token"] == TOKEN
    restored = MacChatData(config_path=path)
    assert restored.configured and restored._token == TOKEN


def test_explicit_cache_answer_never_silently_uses_remote(service):
    service.search({"sent_from": 0}, limit=100)
    service.cache.process()
    service.source.failure = "QUERY_TIMEOUT"
    calls = len(service.source.calls)
    service.model = lambda payload, cancel: {
        "claims": [
            {
                "text": "会议取消，暂不举办",
                "kind": "quote",
                "citations": ["rec:fixture-3"],
            }
        ],
        "unknown": ["缓存覆盖不完整"],
    }
    answer = service.answer(
        "问题", {"sent_from": 0, "keyword": "提议"}, Event(), origin="cache"
    )
    assert answer["origin"] == "cache" and not answer["evidence_complete"]
    assert len(service.source.calls) == calls


@pytest.mark.parametrize(
    "failure,code", [("budget", "MODEL_BUDGET_EXHAUSTED"), ("cancel", "CANCELLED")]
)
def test_real_runner_error_categories_without_model_call(
    service, monkeypatch, failure, code
):
    from allday_asr.v3.adapters.codex.model_execution_runner import (
        ModelExecutionRunner,
        ModelExecutionTerminal,
        ModelExecutionCancelled,
    )

    def reject(*args, **kwargs):
        if failure == "budget":
            raise ModelExecutionTerminal("budget exhausted")
        raise ModelExecutionCancelled("cancelled")

    monkeypatch.setattr(ModelExecutionRunner, "run", reject)
    with pytest.raises(ChatDataError, match=code):
        service.answer("问题", {"sent_from": 0, "keyword": "杭电"}, Event())


def test_answer_candidate_cap_does_not_claim_complete_scope(service):
    service.source.records = [
        record(i, "报名讨论", conversation=f"conversation-{i}")
        for i in range(1, 12)
    ]

    def model(payload, cancel):
        assert not payload["evidence_complete"]
        assert payload["known_gaps"]
        assert len(payload["records"]) == 10
        return {"claims": [], "unknown": ["第 11 个会话未覆盖，无法确定全部安排。"]}

    service.model = model
    answer = service.answer("全部报名安排", {"sent_from": 0, "keyword": "报名"}, Event())
    assert not answer["evidence_complete"] and answer["known_gaps"]
