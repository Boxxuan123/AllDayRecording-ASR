import copy
import json
import sys

import pytest

from tests.chat_fixture import FixtureMac, NOW, record
from tools.retest_chat_fixed_scopes import FixedScopeRetest
from allday_asr.v3.ports.chat_data import ChatDataError


SCOPE = {
    "platform": "qq",
    "source_account_id": "source-qq",
    "conversation_id": "group",
    "sent_from": NOW - 60,
    "sent_to": NOW + 3600,
}


class FixedMac(FixtureMac):
    def request(self, route, body=None, **query):
        result = super().request(route, body, **query)
        if route == "/v1/status":
            result["service_version"] = "1.0.1"
        return result


@pytest.mark.parametrize("count", [0, 1, 2, 3, 9])
def test_full_retest_includes_sparse_terminal_page_and_literal_combinations(count):
    source = FixedMac([record(i, "杭电报名 100%_ ' API") for i in range(1, count + 1)])
    result = FixedScopeRetest(source).run([SCOPE])
    assert result["status"] == "FIXED_SCOPES_RETEST_PASS"
    scope = result["scopes"][0]
    assert scope["ordinary"]["records"] == count
    assert scope["snapshot"]["records"] == count
    assert scope["ordinary"]["pages"] == max(1, (count + 1) // 2)
    assert scope["ordinary"]["terminal_empty_checked"]
    # Filtering must use independent cursors, never a differently scoped snapshot.
    assert all(
        not query.get("snapshot_id")
        for route, _, query in source.calls
        if route == "/v1/messages" and "keyword" in query["scope"]
    )
    if count:
        assert scope["combination_nonempty"] >= 5


@pytest.mark.parametrize(
    "failure", ["QUERY_TIMEOUT", "CURSOR_EXPIRED", "SCOPE_MISMATCH"]
)
def test_retest_errors_preserve_exact_code_and_never_claim_pass(failure):
    class Broken(FixedMac):
        def request(self, route, body=None, **query):
            if route == "/v1/messages":
                raise ChatDataError(failure)
            return super().request(route, body, **query)

    result = FixedScopeRetest(Broken()).run([SCOPE])
    assert result["status"] == "BLOCKED" and result["error"] == failure


def test_retest_page_budget_is_partial_failure():
    source = FixedMac([record(i, "报名") for i in range(1, 5)])
    result = FixedScopeRetest(source, max_pages=1).run([SCOPE])
    assert result["status"] == "BLOCKED" and result["error"] == "RETEST_PAGE_BUDGET"


def test_retest_refuses_body_disagreement():
    class Changed(FixedMac):
        def request(self, route, body=None, **query):
            result = super().request(route, body, **query)
            if route == "/v1/messages" and query.get("snapshot_id") and result["items"]:
                result = copy.deepcopy(result)
                result["items"][0]["text"] = "变更的正文"
            return result

    result = FixedScopeRetest(Changed([record(1, "报名")])).run([SCOPE])
    assert result["error"] == "RETEST_BOUNDARIES_DIFFER_OR_BODY_CHANGED"


def test_retest_duplicate_record_is_a_failure():
    class Duplicate(FixedMac):
        def request(self, route, body=None, **query):
            result = super().request(route, body, **query)
            if route == "/v1/messages" and len(result["items"]) == 2:
                result["items"][1] = copy.deepcopy(result["items"][0])
            return result

    result = FixedScopeRetest(Duplicate([record(1, "报名"), record(2, "报名")])).run(
        [SCOPE]
    )
    assert result["error"] == "RETEST_DUPLICATE_RECORD"


def test_smoke_later_timeout_revokes_ready_state(tmp_path, monkeypatch, capsys):
    from tools import check_chat_integration as smoke

    class Broken(FixedMac):
        def __init__(self, **_):
            super().__init__()

        def request(self, route, body=None, **query):
            if route == "/v1/messages":
                raise ChatDataError("QUERY_TIMEOUT")
            return super().request(route, body, **query)

    path = tmp_path / "scope.json"
    path.write_text(json.dumps(SCOPE), encoding="utf-8")
    monkeypatch.setattr(smoke, "MacChatData", Broken)
    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke", "--scope-file", str(path), "--state-dir", str(tmp_path / "state")],
    )
    assert smoke.main() == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and result["error"] == "QUERY_TIMEOUT"


def test_desktop_chat_access_logs_omit_private_query_parameters(capsys):
    from allday_asr.v3.interfaces.desktop_server import V3DesktopRequestHandler

    handler = object.__new__(V3DesktopRequestHandler)
    handler.client_address = ("127.0.0.1", 12345)
    handler.path = "/api/v3/chat/context?record_id=PRIVATE_RECORD_ID&scope=PRIVATE_TEXT"
    handler.command = "GET"
    handler.log_message('"%s" %s %s', "GET " + handler.path + " HTTP/1.1", 200, "-")
    line = capsys.readouterr().out
    assert "PRIVATE" not in line and "?" not in line
    assert "GET /api/v3/chat/context 200" in line
