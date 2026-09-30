"""Mixed failure queries must be disclosed before interpreting profile repair."""

import json

import numpy as np

from tools.analyze_speaker_profile_purity import clean_failure_query_sensitivity


def test_mixed_target_voice_in_failure_query_is_reported_separately():
    neighbor = {"failure_events": [{"failure_id": "failure", "query_sources": ["clean", "mixed"]}]}
    tasks = [{"task_id": "clean", "start_ms": 0, "end_ms": 8000}]
    reviews = {
        "clean": {"action": "submit", "primary_speaker_person_id": "self",
                  "purity": "clean_single", "other_speaker_ids_json": "[]"},
        "mixed": {"action": "submit", "primary_speaker_person_id": "self",
                  "purity": "mixed_overlap", "other_speaker_ids_json": json.dumps(["focus"])},
    }
    refs = {"focus": [np.array([0.0, 1.0])], "other": [np.array([1.0, 0.0])]}
    frozen = {"known_ids": ["focus", "other"], "minimum_speech_seconds": 1.0,
              "G": {"threshold": 0.5, "margin": 0.0},
              "P": {"per_person": {}, "global_fallback": 0.5, "margin": 0.0}}

    result = clean_failure_query_sensitivity(
        neighbor, tasks, reviews, {"clean": np.array([1.0, 0.0])},
        "self", "focus", refs, refs, frozen,
    )

    assert result["status"] == "EXPLORATORY"
    assert result["query_review"][0] == {
        "failure_id": "failure", "query_sources": 2,
        "clean_self_sources": 1, "mixed_sources": 1,
        "mixed_with_focus_sources": 1, "all_clean_self": False,
    }
    assert result["comparison"]["current"]["G"][0]["windows"] == ["clean"]
