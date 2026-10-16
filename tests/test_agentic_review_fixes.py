"""Independent-review regressions: synthetic data, no remote service or model."""

import copy
import json
import threading
import time
from types import SimpleNamespace
import pytest
from tests.test_agentic_recall import lab as lab_fixture, ask, investigator
from tests.test_recall_query_loop import message, DAY
from allday_asr.v3.application.recall_investigation_tools import InvestigationTools
from allday_asr.v3.domain.recall_investigation_scope import remote_grants
from allday_asr.v3.domain.recall_evidence_selection import select_evidence
from allday_asr.v3.ports.chat_data import ChatDataError


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return lab_fixture.__wrapped__(tmp_path, monkeypatch)


CONTACT = dict(
    key="chosen",
    platform="qq",
    source_account_id="owner-qq",
    conversation_id="chosen-c2c",
    conversation_type="c2c",
    sender_account_id="peer",
    names=["海棠"],
)
SCOPE = dict(
    source="qq",
    person_id="",
    sent_from=500,
    sent_to=2000,
    timezone="Asia/Singapore",
    source_contacts=[CONTACT],
)


def remote_tools(records, grants=None, scope=None):
    requests = []

    def request(route, body=None, **params):
        requests.append(params)
        return {"items": copy.deepcopy(records), "has_more": False}

    sources = SimpleNamespace(
        authorized_scopes=lambda s: grants or [],
        validate_record=lambda r: None,
        changed=lambda rows: set(),
        evidence=lambda r, origin: {
            **r,
            "id": "chat:" + r["record_id"],
            "source": r["platform"],
            "revision": 1,
            "speaker": "海棠",
            "quote": r["text"],
            "person_id": "",
            "role": "match",
            "origin": origin,
        },
    )
    result = {
        "_request": {"question": "核对选中会话", "expand": True},
        "scope": copy.deepcopy(scope or SCOPE),
        "evidence": [],
        "claims": [],
        "metrics": dict(retrieval_requests=0, source_requests=0, source_records_read=0),
        "coverage": [],
        "investigation_steps": [],
    }
    service = SimpleNamespace(
        sources=sources,
        source=SimpleNamespace(request=request),
        _checkpoint=lambda *a: None,
    )
    tools = InvestigationTools(
        service,
        result,
        {"_directory": [CONTACT]},
        threading.Event(),
        time.monotonic() + 10,
        2000,
    )
    tools.remote_checked = True
    return tools, requests


def raw(**changes):
    return {
        "record_id": "synthetic",
        "platform": "qq",
        "source_account_id": "owner-qq",
        "conversation_id": "chosen-c2c",
        "conversation_type": "c2c",
        "sender_account_id": "peer",
        "is_from_self": False,
        "sent_at": 1000,
        "text": "虚构记录",
        **changes,
    }


@pytest.mark.parametrize(
    "grants",
    [
        [],
        [{"platform": "qq", "source_account_id": "owner-qq"}],
        [{k: CONTACT[k] for k in ("platform", "source_account_id", "conversation_id")}],
    ],
)
@pytest.mark.parametrize("self_message", [False, True])
def test_empty_cache_or_account_grant_preserves_selected_bilateral_c2c(
    grants, self_message
):
    row = raw(
        sender_account_id="me" if self_message else "peer", is_from_self=self_message
    )
    tools, requests = remote_tools([row], grants)
    assert tools.search_remote(SCOPE, []) == ["chat:synthetic"]
    assert requests[0]["scope"]["conversation_id"] == "chosen-c2c"
    assert "sender_account_id" not in requests[0]["scope"]


@pytest.mark.parametrize(
    "changes",
    [
        {"conversation_id": "other-c2c"},
        {"conversation_id": "other-group", "conversation_type": "group"},
        {"conversation_type": "group"},
        {"sender_account_id": "different-peer"},
        {"source_account_id": "other-owner"},
        {"platform": "wechat"},
        {"sent_at": 3000},
        {"is_from_self": None},
    ],
)
def test_remote_ignoring_scope_never_admits_out_of_scope_record(changes):
    tools, requests = remote_tools([raw(), raw(**changes)])
    with pytest.raises(ChatDataError, match="CONTEXT_IDENTITY_MISMATCH"):
        tools.search_remote(SCOPE, [])
    assert tools.result["evidence"] == []
    assert requests[0]["scope"]["conversation_id"] == "chosen-c2c"
    assert tools.result["metrics"]["source_records_read"] == 2


def test_selected_group_keeps_observed_speakers_without_inventing_roles():
    group = {
        **CONTACT,
        "conversation_id": "chosen-group",
        "conversation_type": "group",
        "sender_account_id": "",
    }
    scope = {**SCOPE, "source_contacts": [group]}
    tools, requests = remote_tools(
        [
            raw(
                conversation_id="chosen-group",
                conversation_type="group",
                sender_account_id="member-x",
            )
        ],
        scope=scope,
    )
    assert tools.search_remote(scope, [])
    assert requests[0]["scope"]["conversation_id"] == "chosen-group"
    assert "group_role" not in tools.result["evidence"][0]


def test_incompatible_grants_never_fallback_to_account_wide_query():
    scope = {
        **SCOPE,
        "source_accounts": [{"platform": "qq", "source_account_id": "other-owner"}],
    }
    tools, requests = remote_tools([raw()], scope=scope)
    assert tools.search_remote(scope, []) == []
    assert requests == [] and tools.result["coverage"]
    assert (
        remote_grants({**SCOPE, "person_id": "confirmed-person"}, [], [CONTACT]) == []
    )
    assert (
        remote_grants(
            SCOPE,
            [
                {
                    "platform": "qq",
                    "source_account_id": "owner-qq",
                    "conversation_id": "other-c2c",
                }
            ],
            [CONTACT],
        )
        == []
    )


def test_sender_constraints_intersect_grants_instead_of_overwriting():
    scope = {**SCOPE, "source_senders": ["different-peer"]}
    assert (
        remote_grants(scope, [{**CONTACT, "sender_account_id": "peer"}], [CONTACT])
        == []
    )


def evidence(i, **changes):
    return {
        "id": f"chat:e{i:02}",
        "quote": f"虚构第{i}项记录",
        "sent_at": 1000 + i,
        "source": "qq",
        "source_account_id": "owner",
        "conversation_id": "event",
        "sender_account_id": "peer",
        "evidence_type": "message",
        **changes,
    }


def test_31_records_keep_seed_relationship_anchor_and_final_update():
    rows = [evidence(i) for i in range(31)]
    rows[0]["quote"] = "最初公告"
    rows[14]["quote"] = "中途改期"
    rows[22]["quote"] = "另一会话提及相同项目"
    rows[30]["quote"] = "最终取消"
    steps = [
        {"hits": [r["id"] for r in rows]},
        {"support": ["chat:e14"], "targets": ["chat:e22"], "hits": []},
    ]
    chosen, selection = select_evidence(rows, steps, [])
    ids = {r["id"] for r in chosen}
    assert {"chat:e00", "chat:e14", "chat:e22", "chat:e30"} <= ids
    assert len(chosen) == 20 and selection["omitted_count"] == 11
    assert selection["required_omitted_count"] == 0
    assert not selection["complete_conclusion_allowed"]
    assert chosen == sorted(chosen, key=lambda r: r["sent_at"])


def test_multiple_sources_competing_states_and_metadata_boundaries_survive():
    rows = [evidence(i, conversation_id="a" if i < 20 else "b") for i in range(40)]
    rows += [
        evidence(40, evidence_type="native_metadata"),
        evidence(41, evidence_type="native_metadata"),
    ]
    chosen, selection = select_evidence(
        rows, [], [{"citations": ["chat:e08", "chat:e25"]}]
    )
    assert {
        "chat:e00",
        "chat:e19",
        "chat:e20",
        "chat:e39",
        "chat:e40",
        "chat:e41",
        "chat:e08",
        "chat:e25",
    } <= {r["id"] for r in chosen}
    assert selection["semantic_changes_verified"] is False
    assert len(chosen) <= 20


@pytest.mark.parametrize("large", [False, True])
def test_unfit_required_evidence_is_reported_without_increasing_limits(large):
    rows = [
        evidence(
            i,
            conversation_id=str(i),
            quote="文" * (30000 if large and i == 0 else 1000),
        )
        for i in range(31)
    ]
    chosen, selection = select_evidence(rows, [{"support": ["chat:e00"]}], [])
    assert len(chosen) <= 20
    assert len(json.dumps(chosen, ensure_ascii=False).encode()) <= 28000
    assert selection["required_omitted_count"] > 0
    assert not selection["complete_conclusion_allowed"]
    if large:
        assert "chat:e00" in selection["required_omitted_ids"]


def test_real_query_marks_incomplete_selection_and_keeps_cross_source_conflict(lab):
    extra = [
        message(
            6000 + i,
            "岬湾观测的补充讨论" + "补充原文" * 200,
            "peer-r",
            conversation="grove" if i < 10 else "inlet",
            kind="group" if i < 10 else "c2c",
            sent=DAY + (40001 + i if i < 10 else 86400 + 36001 + i),
        )
        for i in range(28)
    ]
    lab.source.records.extend(extra)
    lab.cache.receive(lab.source.dataset, extra)
    lab.cache.process(100)
    lab.service.investigator = investigator(lab)
    result = ask(lab)
    assert result["status"] == "partial", result
    assert "冲突" in result["answer"] and "部分证据" in result["answer"]
    assert any("未送入模型" in s for s in result["unknown"])
    assert result["metrics"]["model_calls"] == 2
    assert not result["investigation_steps"][-1]["sufficient"]
    second = lab.model_inputs[-1]
    assert {"rec:fixture-5001", "rec:fixture-5002"} <= {
        r["record_id"] for r in second["records"]
    }
    assert (
        len(second["records"]) <= 48
        and second["evidence_selection"]["omitted_count"] > 0
    )
