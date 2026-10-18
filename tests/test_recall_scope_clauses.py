"""Paired date/room constraints exercise the production entry and wire, never semantic grading."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids


def day(d):
    return int(datetime(2026, 10, d, tzinfo=ZoneInfo("Asia/Singapore")).timestamp())


def scene(**extra):
    v = dict(
        question="晴川在2026年10月12日的云谷摄影群、10月15日的星海摄影群分别发了什么事项？",
        contacts=[
            contact("qq", "room-a", "云谷摄影群", kind="group"),
            contact("qq", "room-b", "星海摄影群", kind="group"),
            contact("qq", "noise", "无关活动群", kind="group"),
        ],
        records=[
            message(
                701, "请先登记镜头。", "actor", "room-a", day=day(12), label="晴川"
            ),
            message(702, "登记后我来回复。", "other", "room-a", minute=1, day=day(12)),
            message(
                703, "请明天归还三脚架。", "actor", "room-b", day=day(15), label="晴川"
            ),
            message(
                704, "还没归还，明天再确认。", "other", "room-b", minute=1, day=day(15)
            ),
            message(
                705,
                "不该读取甲群的另一天。",
                "actor",
                "room-a",
                day=day(15),
                label="晴川",
            ),
            message(
                706,
                "不该读取乙群的另一天。",
                "actor",
                "room-b",
                day=day(12),
                label="晴川",
            ),
            message(
                707, "不该读取中间日期。", "actor", "room-a", day=day(13), label="晴川"
            ),
            message(
                708, "无关群不能成为目标。", "actor", "noise", day=day(15), label="晴川"
            ),
        ],
        protocol_citation_probe=True,
    )
    v.update(extra)
    return v


def assert_joint_reads(run):
    reads = [c[2]["scope"] for c in run["source_queries"] if c[0] == "/v1/messages"]
    assert reads
    for s in reads:
        assert s.get("conversation_id") in {"room-a", "room-b"}
        d = 12 if s["conversation_id"] == "room-a" else 15
        assert day(d) <= s["sent_from"] <= s["sent_to"] < day(d + 1)
    return reads


@pytest.mark.parametrize("reverse", [False, True])
def test_parallel_dated_rooms_reach_one_synthesis_with_context(tmp_path, reverse):
    v = scene(reverse_response_rows=reverse)
    if reverse:
        v["contacts"].reverse()
        v["records"].reverse()
    r = run_scenario(tmp_path, v)
    assert {
        "rec:fixture-701",
        "rec:fixture-702",
        "rec:fixture-703",
        "rec:fixture-704",
    } <= selected_ids(r)
    assert not {
        "rec:fixture-705",
        "rec:fixture-706",
        "rec:fixture-707",
        "rec:fixture-708",
    } & selected_ids(r)
    assert_joint_reads(r)
    assert len(r["wire"]) == 1 and r["result"]["claims"]
    assert not r["internal"]["scope"]["person_id"]
    assert r["internal"]["subject_resolution"]["persistent_person_merge"] is False
    p = json.loads(r["wire"][0]["prompt"])
    for k in ("scope", "requested_scope", "hard_scope"):
        assert len(p[k]["source_windows"]) == 2
        for s in p[k]["source_windows"]:
            assert (
                datetime.fromisoformat(s["sent_from_local"]).timestamp()
                == s["sent_from"]
            )
            assert (
                datetime.fromisoformat(s["sent_to_local"]).timestamp() == s["sent_to"]
            )
    assert len(json.dumps(r["wire"][0], ensure_ascii=False).encode()) < 60000
    assert r["result"]["metrics"]["source_requests"] <= 18
    assert r["result"]["metrics"].get("body_requests", 0) <= 12
    assert r["result"]["metrics"]["source_records_read"] <= 200


def test_joint_window_not_mutable_by_model_time_or_search(tmp_path):
    def decision(p, v):
        if p.get("round") == 1:
            rooms = [
                o["ref"]
                for o in p["objects"]
                if o.get("type") == "contact" and o.get("conversation_type") == "group"
            ][:2]
            v.update(
                mode="search",
                claims=[],
                time=dict(kind="date_range", first="2026-10-01", last="2026-10-16"),
                actions=[
                    dict(
                        tool="search_messages",
                        purpose="Retry broader candidate window",
                        targets=rooms,
                        terms=[],
                        window="discovery",
                        evidence_ids=[],
                    )
                ],
            )
        return v

    r = run_scenario(tmp_path, scene(), decision=decision)
    assert_joint_reads(r)
    assert not {
        "rec:fixture-705",
        "rec:fixture-706",
        "rec:fixture-707",
        "rec:fixture-708",
    } & selected_ids(r)


def test_parallel_groups_without_person_still_keep_both_scopes(tmp_path):
    v = scene(question="2026年10月12日的云谷摄影群、10月15日的星海摄影群分别聊了什么？")
    r = run_scenario(tmp_path, v)
    assert {"rec:fixture-701", "rec:fixture-703"} <= selected_ids(r)
    assert_joint_reads(r)
    assert not r["internal"]["_plan"]["person_mention"]


def test_joint_window_ui_intersection_does_not_restore_other_date(tmp_path):
    v = scene(filters={"sent_from": day(15), "sent_to": day(16) - 1})
    r = run_scenario(tmp_path, v)
    assert not r["wire"] and not selected_ids(r)
    assert not [c for c in r["source_queries"] if c[0] == "/v1/messages"]


def test_same_named_room_in_different_namespace_is_not_guessed(tmp_path):
    v = scene()
    c = contact("qq", "another-room", "云谷摄影群", kind="group")
    c["source_account_id"] = "another-owner"
    v["contacts"].append(c)
    r = run_scenario(tmp_path, v)
    assert not r["wire"] and not selected_ids(r)
    assert not r["internal"]["scope"]["person_id"]


def test_same_sender_label_different_accounts_not_unified(tmp_path):
    v = scene()
    v["records"][2]["sender_account_id"] = "another-actor"
    r = run_scenario(tmp_path, v)
    assert {"rec:fixture-701", "rec:fixture-703"} <= selected_ids(r)
    assert "subject_resolution" not in r["internal"]
    assert not r["result"]["claims"]
    assert not r["internal"]["scope"]["person_id"]


def test_joint_scope_refuses_unpresented_citation(tmp_path):
    r = run_scenario(tmp_path, scene(invalid_probe_citation=True))
    assert {"rec:fixture-701", "rec:fixture-703"} <= selected_ids(r)
    assert not r["result"]["claims"]


def test_joint_scope_original_and_saved_context_never_cross_date(tmp_path):
    r = run_scenario(tmp_path, scene())
    scopes = r["internal"]["_evidence_scopes"]
    a = next(
        e for e in r["internal"]["evidence"] if e["record_id"] == "rec:fixture-701"
    )
    from allday_asr.v3.domain.recall_investigation_scope import inside_root

    assert inside_root(a, scopes[a["id"]], evidence=True)
    for row in r["internal"]["evidence"]:
        if row["record_id"] == "rec:fixture-703":
            assert not inside_root(row, scopes[a["id"]], evidence=True)


def test_unassociated_dates_do_not_turn_into_contiguous_range(tmp_path):
    r = run_scenario(
        tmp_path, scene(question="2026年10月12日和10月15日分别发生了什么？")
    )
    assert not r["wire"] and not selected_ids(r)


def test_paired_explicit_daypart_not_silently_discarded(tmp_path):
    r = run_scenario(
        tmp_path,
        scene(
            question="晴川在2026年10月12日下午的云谷摄影群、10月15日的星海摄影群分别发了什么？"
        ),
    )
    assert not selected_ids(r) and not r["wire"]


def test_preflight_joint_pairs_reject_cross_product(tmp_path):
    r = run_scenario(tmp_path, scene())
    from copy import deepcopy
    from allday_asr.v3.domain.recall_input_validation import prepare_payload

    p = deepcopy(r["inputs"][0])
    row = p["records"][0]
    native = r["internal"]["_citation_handles"][row["id"]][0]
    full = next(e for e in r["internal"]["evidence"] if e["id"] == native)
    row["sent_at"] = day(15 if full["conversation_id"] == "room-a" else 12) + 13 * 3600
    row["sent_at_local"] = datetime.fromtimestamp(
        row["sent_at"], ZoneInfo("Asia/Singapore")
    ).isoformat()
    with pytest.raises(ValueError, match="joint source/date scope"):
        prepare_payload(p)


def test_original_context_filter_enforces_joint_room_date(tmp_path):
    from allday_asr.v3.adapters.sqlite.recall_state import RecallState
    from types import SimpleNamespace

    r = run_scenario(tmp_path, scene())
    rows = scene()["records"]
    fake = SimpleNamespace()
    kept = RecallState.filter_context(
        fake, rows, {**r["internal"]["scope"], "relationship": "group"}
    )
    assert {x["record_id"] for x in kept} == {
        "rec:fixture-701",
        "rec:fixture-702",
        "rec:fixture-703",
        "rec:fixture-704",
    }


def test_related_actor_cannot_read_outside_paired_rooms(tmp_path):
    def decision(p, v):
        if p.get("round") == 1:
            ref = next(r["id"] for r in p["records"] if r["speaker"] == "晴川")
            v.update(
                mode="search",
                claims=[],
                actions=[
                    dict(
                        tool="get_related_conversations",
                        purpose="Observe other authorised candidate rooms",
                        targets=[ref],
                        terms=[],
                        window="discovery",
                        evidence_ids=[ref],
                    )
                ],
            )
        return v

    r = run_scenario(tmp_path, scene(), decision=decision)
    assert_joint_reads(r)
    assert not {
        "rec:fixture-705",
        "rec:fixture-706",
        "rec:fixture-707",
        "rec:fixture-708",
    } & selected_ids(r)


def test_paired_actor_keeps_user_question_and_all_original_context(tmp_path):
    from types import SimpleNamespace
    from allday_asr.v3.adapters.sqlite.recall_state import RecallState

    v = scene()
    v["records"][1].update(
        sender_account_id="self-account", is_from_self=True, sender_display_name="本人"
    )
    r = run_scenario(tmp_path, v)
    assert {
        "rec:fixture-701",
        "rec:fixture-702",
        "rec:fixture-703",
        "rec:fixture-704",
    } <= selected_ids(r)
    anchor = next(
        x for x in r["internal"]["evidence"] if x["record_id"] == "rec:fixture-701"
    )
    actual_scope = r["internal"]["_evidence_scopes"][anchor["id"]]
    context = RecallState.filter_context(SimpleNamespace(), v["records"], actual_scope)
    assert {x["record_id"] for x in context} == {"rec:fixture-701", "rec:fixture-702"}


def test_question_named_platform_is_joint_hard_constraint(tmp_path):
    r = run_scenario(
        tmp_path,
        scene(
            question="微信晴川在2026年10月12日的云谷摄影群、10月15日的星海摄影群分别发了什么事项？"
        ),
    )
    assert not selected_ids(r) and not r["wire"]
    assert not [c for c in r["source_queries"] if c[0] == "/v1/messages"]


def test_actor_platform_cannot_be_reinterpreted_as_other_platform_sender(tmp_path):
    v = scene(
        question="QQ晴川在2026年10月12日的微信云谷摄影群、10月15日的微信星海摄影群分别发了什么事项？"
    )
    for c in v["contacts"]:
        c.update(platform="wechat", source_account_id="source-wechat")
    for x in v["records"]:
        x.update(platform="wechat", source_account_id="source-wechat")
        x["source_locator"]["platform"] = "wechat"
        x["source_locator"]["source_account_id"] = "source-wechat"
    r = run_scenario(tmp_path, v)
    assert not selected_ids(r) and not r["wire"]
    assert not r["internal"]["scope"]["person_id"]
