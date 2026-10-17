"""A room's verified bridge is independent of the topic evidence for a read action."""
from types import SimpleNamespace
import pytest
from allday_asr.v3.application.recall_investigation_relevance import InvestigationRelevance
from tests.recall_scenario_driver import run_scenario, message
from tests.test_recall_acceptance_scenarios import bridge_case
from tests.test_recall_stability_gate import assert_flow


def support(platform="qq", account="owner", sender="actor", self_=False):
    return dict(type="evidence", source=platform, source_account_id=account,
                sender_account_id=sender, source_is_from_self=self_)


def test_room_bridge_is_not_replaced_by_unrelated_topic_support():
    tools = InvestigationRelevance()
    tools.refs = {"bridge": support(), "topic": support(sender="another")}
    target = dict(relation_account=dict(platform="qq", source_account_id="owner",
                                       sender_account_id="actor"),
                  relation_evidence=["bridge"])
    assert tools.branch_supported(target, {"evidence_ids": ["topic"]})


@pytest.mark.parametrize("changed", [dict(platform="wechat"), dict(account="other"),
                                     dict(sender="same-name-other"), dict(self_=True)])
def test_name_and_topic_evidence_cannot_replace_an_absent_native_bridge(changed):
    tools = InvestigationRelevance()
    tools.refs = {"bridge": support(**changed), "topic": support(sender="another")}
    target = dict(relation_account=dict(platform="qq", source_account_id="owner",
                                       sender_account_id="actor"),
                  relation_evidence=["bridge"])
    assert not tools.branch_supported(target, {"evidence_ids": ["topic"]})
    target["relation_evidence"] = ["withdrawn-or-unseen"]
    assert not tools.branch_supported(target, {"evidence_ids": ["topic"]})


def test_full_path_room_topic_read_preserves_bridge_and_reaches_next_request(tmp_path):
    scene = bridge_case()
    scene["explore_related"] = False
    scene["records"].append(message(700, "展台先等下一条通知。", "other-speaker",
                                    "room-target", minute=6, label="Nori"))
    scene["cached_record_ids"].append("rec:fixture-700")
    actions = []

    def decision(payload, default):
        if payload.get("round") == 1:
            room = next(o for o in payload["objects"] if o.get("relation_basis")
                        and any(r.get("conversation_ref") == o["ref"] and
                                r["quote"] == "展台先等下一条通知。"
                                for r in payload["records"]))
            topic = next(r["id"] for r in payload["records"]
                         if r["quote"] == "展台先等下一条通知。")
            assert topic not in room["relation_evidence"]
            actions.append(dict(tool="search_messages", purpose="继续核对同会话展台通知",
                                targets=[room["ref"]], terms=["展台"], window="episode",
                                evidence_ids=[topic]))
            return {**default, "mode": "search", "actions": actions, "unknown": []}
        return default

    run = run_scenario(tmp_path, scene, decision=decision)
    assert actions and len(run["wire"]) == 2
    assert run["result"]["model_status"] != "failed"
    assert_flow(run, {"rec:fixture-700", "rec:fixture-210", "rec:fixture-212"})
