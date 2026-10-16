"""Source-native recall, fictional metadata and messages; no production/model use."""

import copy
import pytest
from tests.test_recall_query_loop import env as base, ask, message, DAY, SUMMARY
from tests.test_history_recall import dump


@pytest.fixture
def native(tmp_path, monkeypatch):
    env = base.__wrapped__(tmp_path, monkeypatch)
    with env.db.transaction() as c:
        c.execute("UPDATE persons SET user_confirmed=0")
    env.contacts = [
        {
            "platform": p,
            "source_account_id": "source-" + p,
            "id": "native-" + p,
            "conversation_type": "c2c",
            "remark": "青岚",
            "nickname": "Cobalt",
            "peer_account_id": "native-peer",
        }
        for i, p in enumerate(["wechat", "qq"])
    ]
    env.source.records = [
        message(
            3100 + i * 10, "请把样品检查安排在周五。", "native-peer", p, "native-" + p
        )
        for i, p in enumerate(["wechat", "qq"])
    ] + [
        message(
            3101 + i * 10,
            "周五十点可以，我来准备清单。",
            "self-account",
            p,
            "native-" + p,
        )
        for i, p in enumerate(["wechat", "qq"])
    ]
    original = env.source.request

    def request(route, body=None, **kwargs):
        if route == "/v1/conversations":
            env.source.calls.append((route, copy.deepcopy(body), copy.deepcopy(kwargs)))
            if env.source.failure:
                from allday_asr.v3.ports.chat_data import ChatDataError

                raise ChatDataError(env.source.failure)
            return {
                "items": [
                    r
                    for r in env.contacts
                    if r["platform"] == kwargs.get("scope", {}).get("platform")
                ],
                "has_more": False,
                "next_cursor": None,
            }
        return original(route, body, **kwargs)

    env.source.request = request

    def model(payload, cancel):
        rows = payload["records"]
        a = next(r for r in rows if not r["effective_is_self"])
        b = next(r for r in rows if r["effective_is_self"])
        return {
            "claims": [
                {"kind": "summary", "text": SUMMARY, "citations": [a["id"], b["id"]]}
            ],
            "unknown": ["只覆盖本次取回的历史。"],
        }

    env.service.model = model
    return env


@pytest.mark.parametrize("platform", ["wechat", "qq"])
@pytest.mark.parametrize("name", ["青岚", "Cobalt"])
def test_native_without_person_card_fetches_bilateral_and_answers(
    native, platform, name
):
    before = (dump(native.db.path), dump(native.cache.path))
    result = ask(native, f"昨天我和{name}聊了什么", source=platform, expand=True)
    assert result["answer"] == SUMMARY and result["model_status"] == "available"
    assert result["scope"]["person_id"] == "" and result["scope"]["source"] == platform
    assert {r["source"] for r in result["evidence"]} == {platform}
    assert {r["effective_is_self"] for r in result["evidence"]} == {False, True}
    assert all(not r["person_id"] for r in result["evidence"])
    assert all(r["conversation_id"] == "native-" + platform for r in result["evidence"])
    assert (dump(native.db.path), dump(native.cache.path)) == before
    assert all(
        "sender_account_id" not in params["scope"]
        for route, _, params in native.source.calls
        if route == "/v1/messages"
    )


def test_same_name_keeps_namespace_and_choose_continues_without_relation_write(native):
    before = (dump(native.db.path), dump(native.cache.path))
    result = ask(native, "昨天我和青岚聊了什么", expand=True)
    assert (
        result["status"] == "needs_clarification"
        and len(result["contact_candidates"]) == 2
    )
    assert result["evidence"] == [] and not any(
        route == "/v1/messages" for route, _, _ in native.source.calls
    )
    chosen = next(r for r in result["contact_candidates"] if r["platform"] == "wechat")
    continued = native.service.execute(
        "phone",
        {
            "action": "choose",
            "query_id": result["query_id"],
            "contact_key": chosen["key"],
        },
    )
    assert continued["answer"] == SUMMARY and continued["scope"]["source"] == "wechat"
    assert all(r["source"] == "wechat" for r in continued["evidence"])
    assert (dump(native.db.path), dump(native.cache.path)) == before
    assert "_request" not in continued and "_plan" not in continued
    with pytest.raises(ValueError):
        native.service.execute(
            "another-owner",
            {
                "action": "choose",
                "query_id": result["query_id"],
                "contact_key": chosen["key"],
            },
        )


def test_fuzzy_proposals_from_real_directory_require_choice(native):
    def planner(payload, cancel):
        keys = [r["key"] for r in payload["contacts"] if r["display_name"] == "青岚"]
        return {
            "intent": "conversation",
            "relationship": "conversation",
            "person_mention": "那位同事",
            "contact_keys": keys,
            "topics": [],
            "time": {"kind": "unchanged", "first": "", "last": ""},
            "clarification": "",
        }

    native.service.planner = planner
    result = ask(native, "昨天我跟那位昵称Cobalt的同事聊了什么", expand=True)
    assert (
        result["status"] == "needs_clarification"
        and len(result["contact_candidates"]) == 2
    )
    assert (
        result["metrics"]["understanding_calls"] == 1
        and result["metrics"]["source_records_read"] == 0
    )
    chosen = result["contact_candidates"][0]
    answer = native.service.execute(
        "phone",
        {
            "action": "choose",
            "query_id": result["query_id"],
            "contact_key": chosen["key"],
        },
    )
    assert answer["answer"] == SUMMARY


def test_model_cannot_invent_contact_or_drop_unknown_person(native):
    native.service.planner = lambda p, c: {
        "intent": "conversation",
        "relationship": "speaker",
        "person_mention": "未知同事",
        "contact_keys": ["fake-id"],
        "topics": ["样品"],
        "time": {"kind": "unchanged", "first": "", "last": ""},
        "clarification": "",
    }
    result = ask(native, "昨天我和未知同事聊了什么", expand=True)
    assert result["status"] == "needs_clarification" and result["evidence"] == []
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)


@pytest.mark.parametrize("mode", ["empty", "offline", "one-sided"])
def test_native_coverage_and_model_failure_do_not_fake_success(native, mode):
    if mode == "empty":
        native.source.records = []
    if mode == "offline":
        native.source.failure = "SOURCE_OFFLINE"
    if mode == "one-sided":
        native.service.model = lambda payload, cancel: {
            "claims": [
                {
                    "kind": "summary",
                    "text": "对方提出周五检查；另一方答复未知。",
                    "citations": [payload["records"][0]["id"]],
                }
            ],
            "unknown": ["仅单方消息"],
        }
        native.source.records = [
            r for r in native.source.records if not r["is_from_self"]
        ]
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    if mode != "one-sided":
        assert result["claims"] == [] and result["model_status"] != "available"
    assert result["status"] in {
        "coverage_insufficient",
        "needs_clarification",
        "partial",
    }
    if mode == "one-sided":
        assert result["model_status"] == "available"
        assert len(result["evidence"]) == 1 and result["metrics"]["model_calls"] == 1
        assert "另一方答复未知" in result["answer"]
        assert any("单方消息" in note for note in result["coverage"])


def test_wrong_person_date_conversation_do_not_enter_answer(native):
    native.source.records += [
        message(
            3200, "错误日期", "native-peer", "wechat", "native-wechat", sent=DAY - 1
        ),
        message(3201, "错误会话", "native-peer", "wechat", "other"),
        message(3202, "错误发送者", "other-peer", "wechat", "native-wechat"),
    ]
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert result["answer"] == SUMMARY
    assert all("错误" not in r["quote"] for r in result["evidence"])


def test_failing_summary_retains_scoped_originals(native):
    native.service.model = lambda p, c: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert result["claims"] == [] and result["model_status"] == "timeout"
    assert len(result["evidence"]) == 2 and "无法生成" in result["answer"]


def test_context_for_native_contact_preserves_scope_and_sender(native):
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    context = native.service.execute(
        "phone",
        {
            "action": "context",
            "query_id": result["query_id"],
            "evidence_id": result["evidence"][0]["id"],
        },
    )
    assert context["status"] != "stale"
    assert all(
        r["source"] == "wechat" and r["conversation_id"] == "native-wechat"
        for r in context.get("evidence", [])
    )


@pytest.mark.parametrize("platform", ["wechat", "qq"])
def test_platform_in_question_is_a_hard_constraint(native, platform):
    label = "微信" if platform == "wechat" else "QQ"
    result = ask(native, f"昨天在{label}我和青岚聊了什么", expand=True)
    assert result["answer"] == SUMMARY and len(result["evidence"]) == 2
    assert all(r["source"] == platform for r in result.get("evidence", []))
    assert not any(
        c["platform"] != platform for c in result.get("contact_candidates", [])
    )


def test_person_relation_does_not_gate_native_self_direction(native):
    with native.cache.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO user_account_links VALUES(?,?,?,?)",
            ("wechat", "source-wechat", "self-account", "alice-person"),
        )
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert result["answer"] == SUMMARY
    assert {r["effective_is_self"] for r in result["evidence"]} == {True, False}
    assert all(not r["person_id"] for r in result["evidence"])


def test_confirmed_person_can_query_linked_native_conversations(native):
    with native.db.transaction() as c:
        c.execute(
            "UPDATE persons SET user_confirmed=1,display_name='青岚' WHERE person_id='alice-person'"
        )
        c.execute(
            "INSERT INTO person_profile_revisions SELECT person_id,revision+1,display_name,'[\"青岚\"]',relationship_labels_json,notes,'native-test',created_at FROM person_profile_revisions WHERE person_id='alice-person' ORDER BY revision DESC LIMIT 1"
        )
    with native.cache.connect() as c:
        for p in ["qq", "wechat"]:
            c.execute(
                "INSERT OR REPLACE INTO user_account_links VALUES(?,?,?,?)",
                (p, "source-" + p, "native-peer", "alice-person"),
            )
    result = ask(native, "昨天我和青岚聊了什么", person_id="alice-person", expand=True)
    assert result["answer"] == SUMMARY and {
        r["source"] for r in result["evidence"]
    } == {"qq", "wechat"}


def test_unresolved_time_model_failure_does_not_search_messages(native):
    native.service.planner = lambda p, c: (_ for _ in ()).throw(TimeoutError())
    result = ask(native, "上个月我跟青岚聊了什么", source="wechat", expand=True)
    assert result["status"] == "needs_clarification" and result["evidence"] == []
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)


def test_model_can_interpret_topic_without_inventing_account_ids(native):
    def planner(p, c):
        return {
            "intent": "conversation",
            "relationship": "topic",
            "person_mention": "",
            "contact_keys": [],
            "topics": ["样品"],
            "time": {"kind": "unchanged", "first": "", "last": ""},
            "clarification": "",
        }

    native.service.planner = planner
    result = ask(native, "我什么时候和别人讨论过样品", expand=True)
    assert result["terms"] == ["样品"] and result["scope"]["person_id"] == ""
    assert result["scope"]["source_accounts"]
    assert result["metrics"]["understanding_calls"] == 1
    assert all(
        s["source_account_id"].startswith("source-")
        for s in result["scope"]["source_accounts"]
    )


@pytest.mark.parametrize(
    "question",
    [
        "昨天我和青岚聊了什么",
        "我昨天和青岚聊了什么",
        "昨天我们和青岚聊了什么",
        "昨天我和青岚说了啥",
        "昨天我跟Cobalt谈了什么",
    ],
)
def test_colloquial_fast_parse_without_model_understanding(native, question):
    result = ask(native, question, source="wechat", expand=True)
    assert result["answer"] == SUMMARY and result["terms"] == []
    assert result["metrics"]["understanding_calls"] == 0
    assert result["scope"]["sent_from"] == DAY


def semantic(native, **extra):
    def planner(payload, cancel):
        return {
            "intent": "conversation",
            "relationship": "conversation",
            "person_mention": "青岚",
            "contact_keys": [
                r["key"] for r in payload["contacts"] if r["platform"] == "wechat"
            ],
            "topics": [],
            "time": {"kind": "unchanged", "first": "", "last": ""},
            "clarification": "",
            **extra,
        }

    return planner


def test_semantic_colloquial_syntax_preserves_native_source_and_date(native):
    native.service.planner = semantic(native)
    result = ask(native, "昨天我和青岚在微信谈到了什么", expand=True)
    assert result["answer"] == SUMMARY and result["scope"]["source"] == "wechat"
    assert result["metrics"]["understanding_calls"] == 1
    assert {r["effective_is_self"] for r in result["evidence"]} == {True, False}


def test_semantic_relative_range_is_computed_in_local_calendar(native):
    native.service.planner = semantic(
        native, time={"kind": "offset_days", "first": "-1", "last": "-1"}
    )
    result = ask(native, "昨儿我跟青岚聊了什么", source="wechat", expand=True)
    assert result["answer"] == SUMMARY and result["scope"]["sent_from"] == DAY
    assert result["scope"]["sent_to"] == DAY + 86400 - 1


def test_same_source_duplicate_requires_choice(native):
    native.contacts.append(
        {**native.contacts[0], "id": "native-other", "peer_account_id": "other-peer"}
    )
    result = ask(native, "昨天我跟青岚聊了什么", source="wechat", expand=True)
    assert (
        result["status"] == "needs_clarification"
        and len(result["contact_candidates"]) == 2
    )
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)


def test_recording_question_does_not_resolve_native_chat_contact(native):
    result = ask(native, "昨天录音里青岚说了什么", expand=True)
    assert result["evidence"] == [] and result["scope"]["source"] == "recording"
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)


def test_followup_uses_owned_previous_query_without_writing_relations(native):
    first = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    before = dump(native.db.path)
    native.service.planner = semantic(
        native, person_mention="", contact_keys=[], intent="latest"
    )
    result = ask(
        native,
        "上次那件事后来怎么样了",
        source="wechat",
        expand=True,
        previous_query_id=first["query_id"],
    )
    assert result["answer"] == SUMMARY
    assert result["scope"]["source_contacts"] == first["scope"]["source_contacts"]
    assert dump(native.db.path) == before
    other = native.service.execute(
        "other",
        {
            "action": "query",
            "query_id": "other-followup",
            "question": "上次那件事后来怎么样了",
            "source": "wechat",
            "timezone": "Asia/Singapore",
            "previous_query_id": first["query_id"],
            "expand": True,
        },
    )
    assert other["status"] == "needs_clarification" and other["evidence"] == []


def test_expired_context_does_not_block_independent_question(native):
    result = ask(
        native,
        "昨天我和青岚聊了什么",
        source="wechat",
        expand=True,
        previous_query_id="expired-context",
    )
    assert result["answer"] == SUMMARY


def test_partial_directory_resumes_bounded_pages_without_false_ambiguity(native):
    original = native.source.request

    def paged(route, body=None, **params):
        if route != "/v1/conversations":
            return original(route, body, **params)
        native.source.calls.append((route, body, copy.deepcopy(params)))
        cursor = int(params.get("cursor") or 0)
        return {
            "items": [native.contacts[0]],
            "has_more": cursor < 6,
            "next_cursor": str(cursor + 1) if cursor < 6 else None,
        }

    native.source.request = paged
    first = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert (
        first["model_status"] == "available"
        and first["contact_candidates"] == []
    )
    assert first["metrics"]["directory_requests"] <= 6 and first["evidence"]
    assert {e["conversation_id"] for e in first["evidence"]} == {"native-wechat"}
    next_ = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert next_["answer"] == SUMMARY
    assert any(
        p.get("cursor") == "5"
        for route, _, p in native.source.calls
        if route == "/v1/conversations"
    )


def test_native_group_keeps_actual_senders_without_contact_attribution(native):
    native.contacts = [
        {
            **native.contacts[0],
            "id": "research-group",
            "conversation_type": "group",
            "remark": "研究小组",
        }
    ]
    native.source.records = [
        message(
            3800, "样品检查讨论", "someone", "wechat", "research-group", kind="group"
        ),
        message(
            3801,
            "我会补充清单",
            "self-account",
            "wechat",
            "research-group",
            kind="group",
        ),
    ]
    native.service.model = lambda p, c: {
        "claims": [
            {
                "kind": "summary",
                "text": "群里讨论了样品检查。",
                "citations": [p["records"][0]["id"]],
            }
        ],
        "unknown": [],
    }
    result = ask(native, "昨天研究小组聊了什么", source="wechat", expand=True)
    assert result["answer"] == "群里讨论了样品检查。"
    assert len(result["evidence"]) == 2 and all(
        r["speaker"] != "研究小组" for r in result["evidence"]
    )
    assert all(
        not r["person_id"]
        for r in result["evidence"]
        if r["sender_account_id"] == "someone"
    )


def test_native_remote_query_works_without_chat_cache_database(native):
    native.cache.path.unlink()
    result = ask(native, "昨天我和青岚聊了什么", source="wechat", expand=True)
    assert result["answer"] == SUMMARY and len(result["evidence"]) == 2
    assert not native.cache.path.exists()


def test_planning_cancel_stops_before_message_read_and_leaves_no_result(native):
    from threading import Thread, Event

    entered, release = Event(), Event()

    def planner(payload, cancel):
        entered.set()
        assert release.wait(5)
        return semantic(native)(payload, cancel)

    native.service.planner = planner
    results = []
    payload = {
        "action": "query",
        "query_id": "cancel-native-planner",
        "question": "上个月我和青岚聊了什么",
        "source": "wechat",
        "expand": True,
        "timezone": "Asia/Singapore",
    }
    thread = Thread(
        target=lambda: results.append(native.service.execute("phone", payload))
    )
    thread.start()
    assert entered.wait(5)
    native.service.execute(
        "phone", {"action": "cancel", "query_id": payload["query_id"]}
    )
    release.set()
    thread.join(5)
    assert not thread.is_alive() and results[0]["status"] == "cancelled"
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)
    assert ("phone", payload["query_id"]) not in native.service.results


def test_real_but_unrelated_model_candidates_are_not_recommended(native):
    native.service.planner = semantic(native, person_mention="陌生同事")
    result = ask(native, "昨天我跟陌生同事聊了什么", source="wechat", expand=True)
    assert (
        result["status"] == "needs_clarification" and result["contact_candidates"] == []
    )
    assert result["evidence"] == []
    assert not any(route == "/v1/messages" for route, _, _ in native.source.calls)


def test_semantic_proposal_cannot_replace_question_resolved_person(native):
    with native.db.transaction() as c:
        c.execute("UPDATE persons SET user_confirmed=1 WHERE person_id='alice-person'")
    native.service.planner = semantic(native, person_mention="蓝老师")
    result = ask(native, "昨天我和蓝老师为什么选这个时间", expand=True)
    assert result["scope"]["person_id"] == "alice-person"
    assert not result["scope"].get("source_contacts")
    assert result["contact_candidates"] == []
    assert all(
        r["conversation_id"] not in {"native-qq", "native-wechat"}
        for r in result["evidence"]
    )
