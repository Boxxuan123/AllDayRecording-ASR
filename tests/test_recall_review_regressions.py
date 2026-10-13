"""Independent-review regressions plus held-out fictional words; no real providers."""

import pytest
from tests.chat_fixture import record, NOW
from tests.test_history_recall import env as recall_fixture, query, decision, dump
from tests.test_recall_corrections import save, echo
from tests.test_cross_day_memory_quality import row
from tests.test_cross_day_memory_v1 import Sources
from allday_asr.v3.domain.recall_language import plan
from allday_asr.v3.domain.discussion_boundaries import split
from allday_asr.v3.domain.memory_content import continuation_reason
from allday_asr.v3.domain.cross_day_memory import project


@pytest.fixture
def env(tmp_path):
    return recall_fixture.__wrapped__(tmp_path)


@pytest.mark.parametrize(
    "first,second",
    [
        ("我们准备报名计算机考试", "计算机上不了网了"),
        ("图书馆讲座已经报名", "图书馆空调需要维修"),
        ("海边温室的种子已经发芽", "温室参观门票已经售罄"),
        ("准备寄送陶笛样品", "陶笛样品照片想印在海报上"),
        ("这次讨论了冰箱节能设计", "冰箱坏了"),
        ("咖啡机的展览预约已经确认", "这台咖啡机电源坏了"),
    ],
)
def test_shared_noun_does_not_merge_segments_or_create_same_event_cards(first, second):
    # Shared nouns are insufficient even when short wording was already filtered.
    assert len(split([record(1, first), record(2, second)])) == 2
    a, b = row(first), row(second)
    a["id"], b["id"] = "separate-a", "separate-b"
    assert continuation_reason(a, b) is None
    value = project(Sources().scope, [a, b], [])
    assert value["things"] and not value["candidates"]
    assert all(len(t["discussion_ids"]) == 1 for t in value["things"])
    if len(second) >= 6:  # Existing short-message admission stays unchanged.
        assert len(value["things"]) == 2


@pytest.mark.parametrize("code", ["GX-742", "NZ-391"])
def test_explicit_object_reference_has_positive_and_negative_continuity(code):
    a = record(1, f"申请单 {code} 已登记。")
    b = record(2, f"申请单 {code} 已撤销。")
    joined = split([a, b])
    assert (
        len(joined) == 1 and joined[0]["boundary_basis"] == "explicit_object_reference"
    )
    x, y = row(a["text"]), row(b["text"])
    x["id"], y["id"] = "first", "second"
    assert code in continuation_reason(x, y)
    value = project(Sources().scope, [x, y], [])
    assert len(value["candidates"]) == 1 and len(value["things"]) == 2
    b["text"] = "申请单 ZZ-908 已登记。"
    assert len(split([a, b])) == 2 and continuation_reason(x, row(b["text"])) is None
    b["text"] = f"需要比较 {code} 与 ZZ-908 的区别。"
    assert len(split([a, b])) == 2  # Multiple references are ambiguous.


def test_reply_edge_is_scoped_and_shared_words_cannot_bridge_explicit_segments():
    a = record(1, "请核对试验许可。")
    b = record(2, "材料已经撤回。")
    b["reply"] = {"record_id": a["record_id"]}
    c = record(3, "许可查询网站的搜索栏坏了。")
    assert [len(g["records"]) for g in split([a, b, c])] == [2, 1]
    b["conversation_id"] = "elsewhere"
    assert len(split([a, b])) == 2
    b["conversation_id"] = a["conversation_id"]
    b["sent_at"] = a["sent_at"] + 1801
    assert len(split([a, b])) == 2
    b["sent_at"] = a["sent_at"] - 1
    assert len(split([a, b])) == 2


@pytest.mark.parametrize(
    "question",
    [
        "周二面试后来如何",
        "周二面试后来怎样",
        "试验之后有什么进展",
        "许可后续情况怎么样",
        "返修之后有明确回复吗",
        "展览随后结果如何",
    ],
)
def test_followup_intent_is_subject_independent(question):
    assert plan(question)["intent"] == "latest"


@pytest.mark.parametrize(
    "question,expected",
    [
        ("当时为什么考虑软件工程", "reason"),
        ("后来为什么取消面试", "reason"),
        ("帮我找面试的原话", "quote"),
        ("从报名到结束的整个经过", "timeline"),
        ("请查找之后要买的东西的原话", "quote"),
    ],
)
def test_other_intents_not_absorbed_by_followup_words(question, expected):
    assert plan(question)["intent"] == expected


def test_latest_question_prioritizes_keywordless_remote_followup(env):
    service, cache, db, base, source = env
    decision(service, base[0])
    old = [
        record(200 + i, "周二面试准备记录 " + str(i), conversation="interview")
        for i in range(5)
    ]
    save(cache, source, old)
    latest = record(240, "已经取消，请勿再订车。", conversation="interview")
    source.records.append(latest)
    service.model = echo
    before = dump(db.path), dump(cache.path)
    result = query(
        service,
        question="周二面试后来如何",
        keyword="周二面试",
        source="qq",
        person_id="alice-person",
        sent_to=NOW + 500,
        expand=True,
        summarize=True,
    )
    assert result["intent"] == "latest"
    assert "chat:" + latest["record_id"] in result["metrics"]["model_input_ids"]
    message_calls = [
        params for route, body, params in source.calls if route == "/v1/messages"
    ]
    assert message_calls and "keyword" not in message_calls[0]["scope"]
    assert result["claims"][0]["text"] == latest["text"]
    assert (
        result["metrics"]["source_requests"] <= 6
        and result["metrics"]["source_records_read"] <= 100
    )
    assert (dump(db.path), dump(cache.path)) == before


def test_retrieval_survives_140_generic_lexical_distractors(env):
    service, cache, db, records, source = env
    target = record(
        100, "杭电的师资和就业方向最符合我的要求。", conversation="important"
    )
    distractors = [
        record(500 + i, f"我正在考虑其他生活安排：编号{i}。", conversation=f"other{i}")
        for i in range(140)
    ]
    save(cache, source, [target, *distractors])
    result = query(
        service,
        question="帮我回忆一下当时为什么考虑杭电",
        keyword="",
        source="qq",
        sent_from=NOW - 10,
        sent_to=NOW + 1000,
        summarize=False,
        expand=False,
    )
    assert "chat:" + target["record_id"] in {
        e["id"] for e in service._saved("phone", result["query_id"])["evidence"]
    }
