"""Whole-product counterfactual release gate. Protocol probes never count as semantic success."""

import json
import random
import pytest
from tests.test_recall_acceptance_scenarios import bridge_case, plan_case
from tests.recall_scenario_driver import (
    run_scenario,
    selected_ids,
    contact,
    message,
)


def assert_flow(run, required=(), forbidden=()):
    selected = selected_ids(run)
    received = set(run["stage_ids"].get("source_returned", []))
    accepted = set(run["stage_ids"].get("application_accepted", []))
    assert set(required) <= received & accepted & selected
    assert not set(forbidden) & selected
    cited = {
        c.removeprefix("chat:")
        for claim in run["result"]["claims"]
        for c in claim["citations"]
    }
    assert cited <= selected
    assert all(
        len(json.dumps(w, ensure_ascii=False).encode()) <= 60000 for w in run["wire"]
    )
    assert run["semantic_verified"] is False
    return {
        "source_returned": sorted(received),
        "accepted": sorted(accepted),
        "model_selected": sorted(selected),
        "final_citations": sorted(cited),
    }


def rename(scene, seed):
    """Evaluator-side bijection; no answer IDs or naming sent to product code."""
    rng = random.Random(seed)
    mapping = {"HERA": "ORBITAL", "Mira": "陌生发言者"}
    fields = {
        "id",
        "source_account_id",
        "sender_account_id",
        "conversation_id",
        "record_id",
        "native_message_id",
        "peer_account_id",
    }

    def collect(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in fields and isinstance(v, str):
                    mapping.setdefault(
                        v,
                        ("rec:" if k == "record_id" else "anon-")
                        + str(rng.getrandbits(96)),
                    )
                collect(v)
        elif isinstance(x, list):
            for v in x:
                collect(v)

    collect(scene)

    def convert(x):
        if isinstance(x, dict):
            return {k: convert(v) for k, v in x.items()}
        if isinstance(x, list):
            return [convert(v) for v in x]
        if isinstance(x, str):
            return mapping.get(
                x, x.replace("HERA", "ORBITAL").replace("Mira", "陌生发言者")
            )
        return x

    return convert(scene), mapping


@pytest.mark.parametrize("seed", [7, 19, 41])
def test_full_chain_renamed_paginated_interference(tmp_path, seed):
    scene, mapping = rename(bridge_case(), seed)
    random.Random(seed).shuffle(scene["contacts"])
    random.Random(seed + 1).shuffle(scene["records"])
    scene.update(
        directory_page_size=2, reverse_response_rows=True, protocol_citation_probe=True
    )
    core = {mapping[f"rec:fixture-{210 + i}"] for i in range(5)}
    result = run_scenario(tmp_path, scene)
    assert_flow(result, core)
    assert result["result"]["claims"]
    assert not result["internal"]["scope"].get("person_id")


def test_full_chain_transferred_bridge_cannot_reach_old_target(tmp_path):
    scene, mapping = rename(bridge_case(broken=True), 73)
    scene["protocol_citation_probe"] = True
    assert_flow(
        run_scenario(tmp_path, scene),
        forbidden={mapping[f"rec:fixture-{210 + i}"] for i in range(5)},
    )


@pytest.mark.parametrize("seed", [3, 13])
def test_full_chain_daytime_ellipsis_and_cross_namespace(tmp_path, seed):
    scene = plan_case()
    scene["protocol_citation_probe"] = True
    # A participant list/name is not proof that this account spoke or planned anything.
    scene["contacts"].append(contact("qq", "other-owner-room", "林岑", "other-person"))
    scene["contacts"][-1]["source_account_id"] = "separate-owner"
    decoy = message(
        80,
        "今晚已经到商场了。",
        "other-person",
        "other-owner-room",
        kind="c2c",
        label="林岑",
    )
    decoy["source_account_id"] = "separate-owner"
    decoy["source_locator"]["source_account_id"] = "separate-owner"
    scene["records"].append(decoy)
    random.Random(seed).shuffle(scene["contacts"])
    random.Random(seed).shuffle(scene["records"])
    run = run_scenario(tmp_path, scene)
    assert_flow(run, {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"})
    assert not run["internal"]["scope"].get("person_id")
    for wire in run["wire"]:
        payload = json.loads(wire["prompt"])
        original = {e["id"]: e for e in run["internal"]["evidence"]}
        for row in payload["records"]:
            full = run["internal"]["_citation_handles"][row["id"]][0]
            assert row["source_account_id"] == original[full]["source_account_id"]


def test_full_chain_absent_speaker_no_fabricated_plan(tmp_path):
    scene = plan_case()
    scene["records"] = scene["records"][:3]
    result = run_scenario(tmp_path, scene)
    assert_flow(result)
    assert not result["result"]["claims"]
    assert result["result"]["unknown"]


def test_full_chain_unpresented_citation_stays_rejected(tmp_path):
    scene = plan_case()
    scene.update(protocol_citation_probe=True, invalid_probe_citation=True)
    run = run_scenario(tmp_path, scene)
    assert not run["result"]["claims"]
    assert "E999999" not in json.dumps(run["result"].get("claims"))
    assert_flow(run, {"rec:fixture-3", "rec:fixture-4", "rec:fixture-5"})
