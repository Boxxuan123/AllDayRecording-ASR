"""Unrelated synthetic acceptance and counterfactuals; fake transport is NOT semantic success."""

from copy import deepcopy
import random
from tests.recall_scenario_driver import (
    contact,
    message,
    run_scenario,
    selected_ids,
    DAY,
)
from allday_asr.v3.domain.recall_evidence_selection import select_evidence


def plan_case():
    return dict(
        question="昨天晚上林岑打算去哪里",
        contacts=[
            contact("wechat", "wx-room", "林岑", "wx-peer"),
            contact("qq", "qq-room", "同学林岑", "qq-peer"),
            contact("qq", "workshop", "读书小组", kind="group"),
        ],
        records=[
            message(
                1,
                "去年的培训资料先留着。",
                "wx-peer",
                "wx-room",
                "wechat",
                day=DAY - 180 * 86400,
                kind="c2c",
                label="林岑",
            ),
            message(
                2,
                "今天的借阅通知已经看过了。",
                "qq-peer",
                "qq-room",
                kind="c2c",
                label="lin",
            ),
            message(3, "晚一点你去哪儿？", "other", "workshop", minute=1),
            message(
                4,
                "图书馆旁的咖啡馆吧，还没动身。",
                "qq-peer",
                "workshop",
                minute=2,
                label="lin",
            ),
            message(
                5,
                "如果下雨就不出门，先待定。",
                "qq-peer",
                "workshop",
                minute=3,
                label="lin",
            ),
        ],
    )


def test_cross_source_person_lead_and_daytime_plan_reaches_actual_request(tmp_path):
    scenario = plan_case()
    r = run_scenario(tmp_path, scenario)
    assert {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"} <= selected_ids(r)
    assert not r["result"]["contact_candidates"]
    assert r["wire"] and all(
        len(__import__("json").dumps(w, ensure_ascii=False).encode()) <= 60000
        for w in r["wire"]
    )
    assert not r["result"][
        "claims"
    ]  # Offline probe never manufactures a semantic answer.


def test_explicit_platform_cannot_be_relaxed_for_recall(tmp_path):
    scenario = plan_case()
    scenario["filters"] = {"source": "wechat"}
    r = run_scenario(tmp_path, scenario)
    assert all(
        x[2].get("scope", {}).get("platform") != "qq" for x in r["source_queries"]
    )
    assert not ({"rec:fixture-4", "rec:fixture-5"} & selected_ids(r))


def test_episode_density_cannot_evict_relevant_middle_question_and_answer():
    def row(i, t, room, text):
        return dict(
            id=str(i),
            record_id=str(i),
            revision=1,
            source="qq",
            source_account_id="owner",
            conversation_id=room,
            sender_account_id="peer",
            quote=text,
            sent_at=t,
        )

    core = [
        row(100 + i, 1050 + i, "target", s)
        for i, s in enumerate(
            [
                "晚上的展板放哪里？",
                "先放北侧入口",
                "不，入口临时封了",
                "换到南厅签到台旁",
                "收到，等最终通知",
            ]
        )
    ]
    noise = [row(i, 1000 + i, "noisy", "游戏闲聊" * 15) for i in range(90)]
    rows = noise + core
    chosen, stats = select_evidence(
        rows,
        [],
        [],
        limit=20,
        byte_limit=16000,
        focus_scope=dict(
            sent_from=900,
            sent_to=1200,
            question="昨天晚上的展板最后安排在哪里",
            terms=["展板"],
        ),
        seen_ids=[],
    )
    assert {r["id"] for r in core} <= {r["id"] for r in chosen}
    random.Random(71).shuffle(rows)
    assert (
        select_evidence(
            rows,
            [],
            [],
            limit=20,
            byte_limit=16000,
            focus_scope=dict(
                sent_from=900,
                sent_to=1200,
                question="昨天晚上的展板最后安排在哪里",
                terms=["展板"],
            ),
            seen_ids=[],
        )[0]
        == chosen
    )


def bridge_case(broken=False):
    contacts = [
        contact("qq", "lab", "HERA评审组", kind="group"),
        contact("qq", "room-target", "三人讨论", kind="group"),
        contact("qq", "room-noise", "闲聊群", kind="group"),
    ]
    rows = [
        message(
            200,
            "请看通知，有问题稍后讨论。",
            "actor-hera",
            "lab",
            day=DAY - 5 * 86400,
            label="Mira",
        )
    ]
    rows += [
        message(
            210 + i,
            t,
            "actor-other" if broken else "actor-hera",
            "room-target",
            minute=i,
            label="Mira",
        )
        for i, t in enumerate(
            [
                "展台最后放在哪里？",
                "先看北门",
                "北门封了，移到南厅",
                "尚未执行，等开放通知",
                "我记下了",
            ]
        )
    ]
    rows += [
        message(
            300 + i,
            "游戏闲聊，其他话题" * 8,
            "actor-hera",
            "room-noise",
            hour=15,
            minute=i // 2,
            label="Mira",
        )
        for i in range(90)
    ]
    return dict(
        question="昨天晚上HERA发生了什么",
        contacts=contacts,
        records=rows,
        explore_related=True,
        cached_record_ids=[r["record_id"] for r in rows],
    )


def test_d3_interference_received_and_accepted_but_must_reach_actual_request(tmp_path):
    r = run_scenario(tmp_path, bridge_case())
    core = {f"rec:fixture-{210 + i}" for i in range(5)}
    accepted = set(r["stage_ids"]["application_accepted"])
    assert (
        core <= accepted
    )  # This counterexample must expose selection, not missing source data.
    assert core <= selected_ids(r)


def test_same_names_without_native_bridge_do_not_authorize_target_room(tmp_path):
    r = run_scenario(tmp_path, bridge_case(broken=True))
    assert not {f"rec:fixture-{210 + i}" for i in range(5)} & selected_ids(r)


def test_daytime_source_messages_stay_outside_explicit_evening_hard_filter(tmp_path):
    scene = plan_case()
    scene["filters"] = {"sent_from": DAY + 18 * 3600, "sent_to": DAY + 24 * 3600 - 1}
    r = run_scenario(tmp_path, scene)
    assert not {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"} & selected_ids(r)


def test_renaming_every_native_identity_preserves_full_chain_structure(tmp_path):
    import uuid

    scene = bridge_case()
    mapping = {"HERA": "VEGA", "Mira": "木槿"}
    for c in scene["contacts"]:
        for k in ["id", "source_account_id"]:
            mapping[c[k]] = "native-" + uuid.uuid4().hex
    for r in scene["records"]:
        mapping.setdefault(r["sender_account_id"], "sender-" + uuid.uuid4().hex)
        mapping[r["record_id"]] = "rec:" + uuid.uuid4().hex

    def change(v):
        if isinstance(v, dict):
            return {k: change(x) for k, x in v.items()}
        if isinstance(v, list):
            return [change(x) for x in v]
        if isinstance(v, str):
            if v in mapping:
                return mapping[v]
            for a, b in mapping.items():
                if a == "HERA" or a == "Mira":
                    v = v.replace(a, b)
        return v

    renamed = change(scene)
    random.Random(92).shuffle(renamed["contacts"])
    r = run_scenario(tmp_path, renamed)
    assert {mapping[f"rec:fixture-{210 + i}"] for i in range(5)} <= selected_ids(r)


def test_key_discussion_in_middle_of_same_long_room_keeps_question_and_correction():
    rows = []
    for i in range(100):
        rows.append(
            dict(
                id=str(i),
                record_id=str(i),
                revision=1,
                source="qq",
                source_account_id="owner",
                conversation_id="one-room",
                sender_account_id=str(i % 2),
                sent_at=1000 + i,
                quote="无关长聊" + str(i),
            )
        )
    for i, t in enumerate(
        [
            "会议展板最后放在哪里？",
            "原来北门",
            "北门取消了，换成南厅签到处",
            "尚未执行，等确认",
        ]
    ):
        rows[50 + i]["quote"] = t
    selected, _ = select_evidence(
        rows,
        [],
        [],
        limit=20,
        byte_limit=16000,
        focus_scope=dict(
            sent_from=900, sent_to=1200, question="会议展板怎么安排", terms=["展板"]
        ),
    )
    assert {str(i) for i in range(50, 54)} <= {r["id"] for r in selected}


def test_explicit_authorization_revoke_blocks_cached_body_and_saved_answer(tmp_path):
    scene = plan_case()
    scene.update(
        question="昨天我和同学林岑聊了什么",
        revoke_after_first=True,
        cached_record_ids=[r["record_id"] for r in scene["records"]],
    )
    r = run_scenario(tmp_path, scene)
    assert r["revoked"]["query"].get("status") == 403
    assert r["revoked"]["page"].get("status") == 403
    assert r["revoked"]["extra_model_calls"] == 0


def test_native_peer_missing_uses_bounded_history_only_as_actor_bridge(tmp_path):
    scene = plan_case()
    scene["contacts"][1]["peer_account_id"] = None
    scene["records"][1]["sent_at"] = DAY - 10 * 86400 + 13 * 3600
    r = run_scenario(tmp_path, scene)
    assert {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"} <= selected_ids(r)
    assert not r["result"]["contact_candidates"]


def test_other_platform_different_nickname_uses_direct_address_not_name_merge(tmp_path):
    scene = plan_case()
    scene["contacts"][1]["display_name"] = "山雨"
    scene["records"].extend(
        [
            message(
                50,
                "林岑，你的借阅证找到没？",
                "self-account",
                "qq-room",
                kind="c2c",
                day=DAY - 7 * 86400,
            ),
            message(
                51,
                "找到了，谢谢提醒。",
                "qq-peer",
                "qq-room",
                minute=1,
                kind="c2c",
                day=DAY - 7 * 86400,
            ),
        ]
    )
    r = run_scenario(tmp_path, scene)
    assert {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"} <= selected_ids(r)
    assert r["internal"].get("source_candidate_relations")
    assert not r["internal"]["scope"].get("person_id")


def test_actual_request_episode_metadata_only_describes_selected_originals(tmp_path):
    import json

    scene = bridge_case()
    r = run_scenario(tmp_path, scene)
    for wire in r["wire"]:
        payload = json.loads(wire["prompt"])
        rows = {v["id"]: v for v in payload["records"]}
        for episode in payload["episode_candidates"]:
            assert (
                set(episode["record_refs"] + episode["relation_support"]) <= rows.keys()
            )
            assert episode["message_first"] == min(
                rows[h]["sent_at"] for h in episode["record_refs"]
            )
            assert episode["message_last"] == max(
                rows[h]["sent_at"] for h in episode["record_refs"]
            )
        assert len(json.dumps(wire, ensure_ascii=False).encode()) <= 60000


def test_episode_window_or_reference_corruption_stops_before_executor(tmp_path):
    import json
    import pytest
    from allday_asr.v3.domain.recall_input_validation import prepare_payload

    r = run_scenario(tmp_path, plan_case())
    payload = json.loads(r["wire"][0]["prompt"])
    for mutate in [
        lambda p: p["episode_candidates"][0].update(
            message_first_local="2026-01-01T00:00:00+08:00"
        ),
        lambda p: p["episode_candidates"][0]["record_refs"].append("E999"),
        lambda p: p["query_reference"].update(unix=0),
    ]:
        broken = deepcopy(payload)
        mutate(broken)
        with pytest.raises(ValueError):
            prepare_payload(broken)


def test_exact_native_group_label_routes_as_topic_and_follows_observed_senders(
    tmp_path,
):
    scene = bridge_case()
    scene["question"] = "昨天晚上HERA评审组发生了什么"
    r = run_scenario(tmp_path, scene)
    assert {f"rec:fixture-{210 + i}" for i in range(5)} <= selected_ids(r)
    assert r["internal"]["_plan"]["person_mention"] == ""
    assert r["internal"]["scope"]["relationship"] == "topic"


def test_relation_enrichment_preserves_native_label_and_peer_namespace():
    import pytest
    from allday_asr.v3.application.recall_investigation_tools import InvestigationTools

    tools = InvestigationTools.__new__(InvestigationTools)
    tools.refs = {}
    c = dict(
        platform="qq",
        source_account_id="account-A",
        conversation_id="native-room",
        sender_account_id="peer-A",
        display_name="当前目录名称",
        names=["当前目录名称"],
        basis="native conversation directory",
    )
    ref = tools.ref("contact", c, "stable-key")
    tools.ref(
        "contact",
        {
            **c,
            "sender_account_id": None,
            "display_name": "旧标签",
            "names": ["旧标签"],
            "relation_basis": "observed_same_source_account_speech",
        },
        "stable-key",
    )
    assert tools.refs[ref]["display_name"] == "当前目录名称"
    assert tools.refs[ref]["sender_account_id"] == "peer-A"
    assert tools.refs[ref]["names"] == ["当前目录名称", "旧标签"]
    with pytest.raises(ValueError, match="namespace changed"):
        tools.ref("contact", {**c, "source_account_id": "account-B"}, "stable-key")
    with pytest.raises(ValueError, match="namespace changed"):
        tools.ref(
            "contact",
            {**c, "sender_account_id": "same-name-different-peer"},
            "stable-key",
        )
