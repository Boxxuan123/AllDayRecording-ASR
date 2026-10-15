"""Root-scope regression only; synthetic adapters and existing cache fixtures."""

import copy
import threading
import time
import pytest
from tests.test_agentic_review_fixes import CONTACT, SCOPE, remote_tools, raw
from tests.test_agentic_recall import lab as lab_fixture, ask, investigator, action
from allday_asr.v3.domain.recall_investigation_scope import intersect_root
from allday_asr.v3.application.recall_investigation_tools import InvestigationTools


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return lab_fixture.__wrapped__(tmp_path, monkeypatch)


@pytest.mark.parametrize("remote", [False, True])
def test_model_target_cannot_replace_original_contact_even_with_known_reference(remote):
    tools, requests = remote_tools([raw(conversation_id="other-room")])
    tools.result["_request"]["expand"] = remote
    queries = []
    tools.service.sources.chat = lambda *a: (queries.append(a[0]) or [], False)
    other = {
        **CONTACT,
        "key": "other",
        "conversation_id": "other-room",
        "type": "contact",
    }
    # Even if a later relation registers an authorized-but-outside-query reference,
    # the execution guard must reject it before local or remote I/O.
    tools.ref("contact", other, "other")
    assert tools.run(action("search_messages", ["contact:other"]), []) == []
    assert queries == [] and requests == [] and tools.result["evidence"] == []
    assert any("硬筛选" in line for line in tools.result["coverage"])


def test_root_snapshot_cannot_be_changed_via_result_or_dynamic_scope():
    tools, requests = remote_tools([raw(conversation_id="other-room")])
    tools.result["scope"]["source_contacts"][0]["conversation_id"] = "other-room"
    assert tools.search_remote(tools.result["scope"], []) == []
    assert tools.root_scope["source_contacts"][0]["conversation_id"] == "chosen-c2c"
    assert requests == []


@pytest.mark.parametrize("origin", ["cache", "mac"])
def test_context_and_direct_evidence_outside_root_are_not_read_or_admitted(origin):
    tools, requests = remote_tools([])
    context_calls = []
    tools.service.sources.context = lambda *a: context_calls.append(a) or []
    row = tools.service.sources.evidence(raw(conversation_id="other-room"), origin)
    assert tools.context([{"type": "evidence", **row}], copy.deepcopy(SCOPE)) == []
    assert tools.add([row]) == []
    assert not context_calls and not requests and not tools.result["evidence"]


def test_same_target_and_bilateral_messages_still_work():
    tools, requests = remote_tools([raw()])
    tools.service.sources.chat = lambda *a: ([], False)
    assert tools.run(action("search_messages", ["contact:chosen"]), [])
    assert requests[0]["scope"]["conversation_id"] == "chosen-c2c"
    assert tools.result["evidence"][0]["conversation_id"] == "chosen-c2c"
    saved = tools.result["_evidence_scopes"]["chat:synthetic"]
    assert saved["source_contacts"][0]["conversation_id"] == "chosen-c2c"


def test_root_intersects_platform_person_account_and_explicit_date():
    root = {
        **SCOPE,
        "source_accounts": [{"platform": "qq", "source_account_id": "owner-qq"}],
    }
    for changed in [
        dict(source="wechat"),
        dict(source_accounts=[{"platform": "qq", "source_account_id": "other"}]),
        dict(sent_from=3000, sent_to=4000),
    ]:
        assert intersect_root(root, {**SCOPE, **changed}) is None
    assert (
        intersect_root(
            {**root, "person_id": "confirmed"}, {**SCOPE, "person_id": "other"}
        )
        is None
    )
    merged = intersect_root(root, {**SCOPE, "sent_from": 0, "sent_to": 10000})
    assert (merged["sent_from"], merged["sent_to"]) == (500, 2000)


def test_related_cache_query_obeys_hard_contact_without_disabling_unfiltered_relation(
    lab,
):
    target = dict(
        platform="qq", source_account_id="source-qq", sender_account_id="peer-r"
    )
    scope = dict(
        source="qq",
        person_id="",
        sent_from=0,
        sent_to=2000000000,
        timezone="Asia/Singapore",
    )
    rows, _ = lab.service.sources.related_conversations(
        target, scope, threading.Event(), time.monotonic() + 5
    )
    assert {r["contact"]["conversation_id"] for r in rows} >= {"grove", "inlet"}
    selected = {
        **scope,
        "source_contacts": [
            dict(platform="qq", source_account_id="source-qq", conversation_id="grove")
        ],
    }
    rows, _ = lab.service.sources.related_conversations(
        target, selected, threading.Event(), time.monotonic() + 5
    )
    assert rows and {r["contact"]["conversation_id"] for r in rows} == {"grove"}


def test_automatic_seed_is_distinct_from_explicit_filter_and_multihop_survives(lab):
    lab.service.investigator = investigator(lab)
    result = ask(lab)
    assert result["model_status"] == "available", result
    assert {r["conversation_id"] for r in result["evidence"]} >= {"grove", "inlet"}
    assert "冲突" in result["answer"] and result["metrics"]["model_calls"] == 2


def test_directory_refs_and_memory_do_not_bypass_hard_contact():
    tools, requests = remote_tools([])
    other = {**CONTACT, "key": "other", "conversation_id": "other-room"}
    root_result = copy.deepcopy(tools.result)
    t = InvestigationTools(
        tools.service,
        root_result,
        {"_directory": [CONTACT, other]},
        threading.Event(),
        time.monotonic() + 10,
        2000,
    )
    assert "contact:other" not in t.refs
    t.service.sources.existing_memory = lambda *a: pytest.fail(
        "hard conversation cannot query global memory"
    )
    assert t.run(action("get_existing_memory", terms=["项目"]), []) == []
    assert requests == []
