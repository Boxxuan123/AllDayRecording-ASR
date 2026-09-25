import json
import sqlite3

import pytest

from allday_asr.v3.domain.sound_kind import sound_uses
from allday_asr.v3.adapters.sqlite.sound_eligibility import usable_content, confirmed_interaction
from tools.evaluate_annotation_benefit import evaluate


@pytest.mark.parametrize("kind,content,sample,source,interaction", [
    ("speech", True, True, "unknown", None),
    ("live_speech", True, True, "live", True),
    ("unintelligible", False, False, "unknown", None),
    ("background_speech", False, False, "unknown", None),
    ("overlapping_speech", True, False, "unknown", None),
    ("remote_speech", True, False, "remote", True),
    ("media_speech", False, False, "media", False),
])
def test_content_identity_and_sample_purposes_are_separate(kind, content, sample, source, interaction):
    evidence = {"sound_kind": kind, "person_annotation": {"person_id": "known"}}
    uses = sound_uses(evidence)
    assert uses["content_usable"] is content
    assert uses["sample_candidate_allowed"] is sample
    assert uses["source"] == source
    assert uses["actual_interaction"] is interaction
    with sqlite3.connect(":memory:") as connection:
        actual = connection.execute(f"SELECT {usable_content('u')} FROM (SELECT ? evidence_json) u",
                                    (json.dumps(evidence),)).fetchone()[0]
    assert bool(actual) is content


@pytest.mark.parametrize("evidence,expected", [({}, False),
    ({"person_annotation": {"person_id": "known"}}, False),
    ({"actual_interaction": True}, True),
    ({"sound_kind": "remote_speech"}, True),
    ({"sound_kind": "live_speech"}, True),
    ({"sound_kind": "media_speech"}, False)])
def test_identity_alone_does_not_establish_an_interaction(evidence, expected):
    with sqlite3.connect(":memory:") as connection:
        result = connection.execute(f"SELECT {confirmed_interaction('u')} FROM (SELECT ? evidence_json) u",
                                    (json.dumps(evidence),)).fetchone()[0]
    assert bool(result) is expected


def test_effect_evaluation_rejects_same_conversation_across_devices():
    manifest = {"model": "fixed", "model_version": "1", "conditions": {"threshold": .9},
        "known_person_ids": ["self"], "self_person_id": "self",
        "enrollment": [{"evidence_id": "watch", "conversation_id": "meeting", "date": "2026-09-01"}]}
    rows = [{"evidence_id": "phone", "conversation_id": "meeting", "date": "2026-09-02",
             "truth_person_id": "self", "before": None, "after": "self"}]
    with pytest.raises(ValueError, match="conversation_id"):
        evaluate(manifest, rows)
    rows[0]["conversation_id"] = "independent"
    result = evaluate(manifest, rows)
    assert result["after"]["known_identity_accuracy"] == 1
    assert result["after"]["stranger_false_known_rate"] is None
    assert result["after"]["downstream"]["memory"]["evaluated"] == 0
