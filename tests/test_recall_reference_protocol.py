"""Query clock is separate from message windows; producer and consumer share exact short handles."""

from copy import deepcopy
import json
from tests.recall_scenario_driver import run_scenario, contact, message, NOW
from allday_asr.v3.adapters.codex.recall_schema import evidence_schema
from allday_asr.v3.domain.recall_answer import SCHEMA as SUMMARY
from allday_asr.v3.domain.recall_investigation import SCHEMA as INVESTIGATION


def test_simple_conversation_actual_request_has_query_reference(tmp_path):
    room = "fictional-room"
    scene = dict(
        question="昨天我和叶岚聊了什么",
        contacts=[contact("wechat", room, "叶岚", "peer")],
        records=[
            message(1, "明晚你方便开会吗", "self-account", room, "wechat", kind="c2c"),
            message(
                2, "先计划，尚未执行", "peer", room, "wechat", minute=1, kind="c2c"
            ),
        ],
        filters={"keyword": ""},
    )
    result = run_scenario(tmp_path, scene)
    wire = next(w for w in result["wire"] if w["task"] == "history-recall-v2")
    data = json.loads(wire["prompt"])
    assert data["query_reference"] == {
        "unix": NOW,
        "timezone": "Asia/Singapore",
        "local": "2026-10-16T08:35:00+08:00",
    }
    assert data["local_now"] == data["query_reference"]["local"]
    assert data["scope"]["sent_to"] < NOW
    assert "Query-relative dates MUST use query_reference" in wire["instructions"]
    assert wire["schema"]["properties"]["claims"]["items"]["properties"]["citations"][
        "items"
    ]["enum"] == [r["id"] for r in data["records"]]


def test_schema_constraints_do_not_mutate_legacy_or_unrelated_string_fields():
    old = deepcopy(INVESTIGATION)
    rows = [{"id": "E2"}, {"id": "E7"}]
    schema = evidence_schema(INVESTIGATION, rows)
    props = schema["properties"]
    claim = props["claims"]["items"]["properties"]
    assert claim["citations"]["items"]["enum"] == ["E2", "E7"]
    assert "E2儀E7" not in claim["citations"]["items"]["enum"]
    assert claim["text"] == old["properties"]["claims"]["items"]["properties"]["text"]
    branches = claim["event_time"]["anyOf"]
    assert branches[0]["properties"]["evidence_id"]["enum"] == [""]
    assert branches[1]["properties"]["evidence_id"]["enum"] == ["E2", "E7"]
    assert props["actions"]["items"]["properties"]["evidence_ids"]["items"]["enum"] == [
        "E2",
        "E7",
    ]
    assert props["actions"]["items"]["properties"]["purpose"] == {"type": "string"}
    assert INVESTIGATION == old
    assert SUMMARY["properties"]["claims"]["items"]["properties"]["citations"][
        "items"
    ] == {"type": "string"}


def test_empty_input_cannot_remove_strict_reference_shape():
    schema = evidence_schema(SUMMARY, [])
    assert (
        schema["properties"]["claims"]["items"]["properties"]["citations"]["items"][
            "pattern"
        ]
        == r"^E[1-9]\d*$"
    )
