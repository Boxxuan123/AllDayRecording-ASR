"""Fictional discovery regressions: native sender metadata, quoted topics and namespace limits."""

import json
import random
import pytest
from tests.recall_scenario_driver import contact, message, run_scenario, selected_ids


def sender_case(noise=0, separate=False):
    scene = dict(
        question="昨天上午林岑在云岭和星海摄影群发了什么",
        contacts=[
            contact("wechat", "ridge", "云岭摄影交流群", kind="group"),
            contact("wechat", "sea", "星海摄影交流群", kind="group"),
            contact("wechat", "decoy", "公路跑团", kind="group"),
        ],
        records=[
            message(
                1,
                "早上好，准备去拍晨雾。",
                "camera-user",
                "ridge",
                "wechat",
                hour=8,
                label="林岑",
            ),
            message(2, "几点出门？", "other", "ridge", "wechat", hour=8, minute=1),
            message(
                3,
                "早，今天还没确定成行。",
                "other-camera" if separate else "camera-user",
                "sea",
                "wechat",
                hour=8,
                label="林岑",
            ),
        ],
    )
    scene["records"] = [
        message(
            1000 + i,
            "早上交通讨论，无关摄影。",
            "bus-user",
            "decoy",
            "wechat",
            hour=8,
            label="无关用户",
        )
        for i in range(noise)
    ] + scene["records"]
    return scene


@pytest.mark.parametrize("seed,noise", [(1, 0), (21, 200), (45, 350)])
def test_named_sender_metadata_survives_unrelated_activity_and_directory_order(
    tmp_path, seed, noise
):
    scene = sender_case(noise)
    random.Random(seed).shuffle(scene["contacts"])
    scene.update(
        reverse_response_rows=True, directory_page_size=2, protocol_citation_probe=True
    )
    run = run_scenario(tmp_path, scene)
    assert {"rec:fixture-1", "rec:fixture-3"} <= selected_ids(run)
    assert run["internal"]["_plan"]["person_mention"] == "林岑"
    assert not run["internal"]["scope"]["person_id"]
    assert run["result"]["metrics"]["source_records_read"] <= 200
    assert run["result"]["metrics"]["source_requests"] <= 18
    assert all(
        len(json.dumps(w, ensure_ascii=False).encode()) <= 60000 for w in run["wire"]
    )
    relations = run["internal"]["sender_candidate_relations"]
    assert len(relations) == 1
    assert relations[0]["persistent_person_merge"] is False
    assert len(relations[0]["evidence_ids"]) == 2


def test_same_name_distinct_native_accounts_never_get_a_shared_sender_bridge(tmp_path):
    run = run_scenario(tmp_path, sender_case(separate=True))
    assert {"rec:fixture-1", "rec:fixture-3"} <= selected_ids(run)
    relations = run["internal"]["sender_candidate_relations"]
    assert len(relations) == 2
    assert len({r["sender_account_id"] for r in relations}) == 2
    assert all(
        len(r["evidence_ids"]) == 1 and not r["persistent_person_merge"]
        for r in relations
    )
    assert not run["internal"]["scope"].get("person_id")


def test_explicit_platform_and_person_boundary_survives_sender_discovery(tmp_path):
    scene = sender_case()
    scene["filters"] = {"source": "qq"}
    run = run_scenario(tmp_path, scene)
    assert not {"rec:fixture-1", "rec:fixture-3"} & selected_ids(run)
    assert all(
        r.get("source") != "wechat"
        for inp in run["inputs"]
        for r in inp.get("records", [])
    )


def quoted_case():
    return dict(
        question="昨天周启说的“青舟”到底是哪位同事",
        contacts=[
            contact("wechat", "private", "周启", "wx-actor"),
            contact("qq", "colleagues", "伙伴交流群", kind="group"),
        ],
        records=[
            message(1, "青舟是哪位同事？", "native-one", "colleagues", hour=9),
            message(
                2,
                "我不知道是谁，只听过这个外号。",
                "native-two",
                "colleagues",
                hour=9,
                minute=1,
            ),
            message(
                3,
                "先别把猜测当实名确认。",
                "native-one",
                "colleagues",
                hour=9,
                minute=2,
            ),
        ],
    )


def test_short_quoted_topic_remains_soft_cross_source_lead_with_adjacent_denial(
    tmp_path,
):
    run = run_scenario(tmp_path, quoted_case())
    assert {"rec:fixture-1", "rec:fixture-2", "rec:fixture-3"} <= selected_ids(run)
    assert run["internal"]["_plan"]["intent"] == "question"
    assert not run["internal"]["scope"]["person_id"]
    assert not run["result"][
        "claims"
    ]  # Protocol-only output is never semantic evidence.


def test_short_quoted_topic_cannot_cross_explicit_source(tmp_path):
    scene = quoted_case()
    scene["filters"] = {"source": "wechat"}
    run = run_scenario(tmp_path, scene)
    assert not {"rec:fixture-1", "rec:fixture-2", "rec:fixture-3"} & selected_ids(run)


def test_native_group_whitespace_is_optional_without_short_latin_capture():
    from allday_asr.v3.domain.recall_query import name_in_text

    assert name_in_text("LYRA 摄影外拍群", "昨天LYRA摄影外拍群聊了什么")
    assert not name_in_text("c", "昨天ACM群发生什么")


def test_question_context_keeps_two_separated_supported_periods():
    from allday_asr.v3.domain.recall_episode_ranking import question_episode_priority

    # Domain inputs are deliberately unrelated to real topics and names.
    rows = []
    for i, (when, text) in enumerate(
        [
            (9, "还不知道QX的外壳材质，如果陶瓷容易磕坏。"),
            (9, "我是在问材质，不是确认。"),
            (14, "我手里这个陶瓷外壳有裂痕。"),
            (14, "QX风口中间环形是陶瓷的，容易挤压。"),
        ]
    ):
        r = message(500 + i, text, "native", "gear", hour=when, minute=i)
        rows.append(
            dict(
                id="chat:" + r["record_id"],
                source="qq",
                source_account_id="source-qq",
                conversation_id="gear",
                sent_at=r["sent_at"],
                quote=text,
            )
        )
    ids = question_episode_priority(
        rows,
        dict(
            question="昨天器材群QX的外壳确认是陶瓷了吗",
            body_question="QX外壳确认陶瓷",
            terms=[],
        ),
    )
    assert "chat:rec:fixture-500" in ids
    assert "chat:rec:fixture-503" in ids
