"""Offline-only evidence, frozen splits, and event-level evaluation contracts."""

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from short_self_dataset import digest, integrity, readonly, split_dates, write
from short_self_evidence import crop_ranges, score_partition, validate_manifest
from short_self_rules import (
    RULES,
    evaluate,
    evidence_status,
    fit,
    metrics,
    would_accept,
)
from tests.test_blind_validation import world as world, seed
from tests.test_self_identity_regression import Provider, anchor
from tests import test_v34_open_speaker_identity as fixtures


def event(truth="self", *, score=0.9, duration=3040, safe=True):
    return {
        "event_id": "synthetic-" + truth,
        "session_id": "synthetic-session",
        "truth": truth,
        "duration_ms": duration,
        "safe_ownership": safe,
        "bin": "3-4",
        "full": {
            "score": score,
            "decision": "self" if score >= 0.8 else "unknown",
            "self_minus_best_other": score - 0.2,
            "reference_consensus": {"support_fraction_at_frozen_threshold": 0.9},
        },
        "crops": [{"score": score}, {"score": score}],
        "crop_score_statistics": {"count": 2, "min": score, "std": 0.0},
        "current_two_window": "unknown",
    }


PARAMS = {
    "T": 0.8,
    "T_short": 0.85,
    "M_short": 0.1,
    "crop_min_passes": 2,
    "crop_std_max": 0.05,
    "min_crop_threshold": 0.85,
    "reference_support_min": 0.8,
}


def manifest(output, events):
    write(output / "split-manifest.json", {"events": events})
    (output / "split-manifest.sha256").write_text(
        digest(output / "split-manifest.json") + "\n"
    )


def test_dates_keep_sessions_together_without_scores():
    rows = [
        {"date": "2026-01-01", "session_id": "same"},
        {"date": "2026-01-01", "session_id": "same"},
        {"date": "2026-01-02", "session_id": "next"},
        {"date": "2026-01-03", "session_id": "last"},
    ]
    splits = split_dates(rows)
    assert splits["2026-01-01"] == "development"
    assert splits["2026-01-03"] == "holdout"
    assert len({splits[e["date"]] for e in rows if e["session_id"] == "same"}) == 1


def test_split_tampering_is_rejected(tmp_path):
    manifest(tmp_path, [])
    validate_manifest(tmp_path)
    write(tmp_path / "split-manifest.json", {"events": [{"unexpected": True}]})
    with pytest.raises(ValueError, match="changed"):
        validate_manifest(tmp_path)


def test_overlapping_crops_are_bounded_and_not_independent_samples():
    crops = crop_ranges(1000, 4040)
    assert len(crops) == 5
    assert all(1000 <= a < b <= 4040 and b - a == 2000 for a, b in crops)
    e = event()
    e["crops"] = [{"score": 0.9} for _ in range(10)]
    result = metrics([e], "crop_stability", PARAMS)
    assert result["positive_events"] == 1 and result["self_accepted"] == 1
    assert not would_accept(e, "current_two_window", PARAMS)


@pytest.mark.parametrize("bounds", [(-1, 3040), (1000, 1000), (1000, 1500)])
def test_malformed_clip_rejected(bounds):
    with pytest.raises(ValueError):
        crop_ranges(*bounds)


@pytest.mark.parametrize("fault", ["missing", "unsafe", "short", "no_margin"])
def test_missing_evidence_remains_unknown(fault):
    e = event()
    if fault == "missing":
        e["full"] = {"reason": "embedding_unavailable", "decision": "unknown"}
    elif fault == "unsafe":
        e["safe_ownership"] = False
    elif fault == "short":
        e["duration_ms"] = 1500
    else:
        e["full"]["self_minus_best_other"] = None
    rule = "score_margin" if fault == "no_margin" else "high_score"
    assert not would_accept(e, rule, PARAMS)


def test_fit_never_reads_reference_anchors_or_holdout_and_is_frozen(tmp_path):
    rows = [event()] + [event("non-self", score=0.2 + i * 0.01) for i in range(5)]
    for i, e in enumerate(rows):
        e.update(event_id=str(i), split="development", strict_independent=False)
    manifest(tmp_path, rows)
    write(
        tmp_path / "development-evidence.json",
        {"matcher": {"self_threshold": 0.8, "reference_count": 10}, "events": rows},
    )
    # Deliberately malformed reference file: fitting must never open it.
    (tmp_path / "reference-evidence.json").write_text("not-json")
    rules = fit(tmp_path)
    assert not rules["production_enabled"]
    assert rules["parameters"]["T_short"] == 0.8
    first = digest(tmp_path / "candidate-rules.json")
    with pytest.raises(ValueError, match="already frozen"):
        fit(tmp_path)
    assert digest(tmp_path / "candidate-rules.json") == first
    reloaded = json.loads((tmp_path / "candidate-rules.json").read_text())
    assert reloaded["rule_version"] == "short-self-shadow-v1"


def test_holdout_runs_once_and_counts_events(tmp_path):
    rows = [event(), event("non-self", score=0.1)]
    manifest(tmp_path, rows)
    write(
        tmp_path / "candidate-rules.json",
        {
            "parameters": PARAMS,
            "rule_version": "fixture-v1",
            "manifest_sha256": digest(tmp_path / "split-manifest.json"),
        },
    )
    write(
        tmp_path / "holdout-evidence.json",
        {"events": rows, "manifest_sha256": digest(tmp_path / "split-manifest.json")},
    )
    result = evaluate(tmp_path, "holdout")
    assert result["high_score"]["self_accepted"] == 1
    assert result["high_score"]["negative_self"] == 0
    first = digest(tmp_path / "holdout-shadow.json")
    with pytest.raises(ValueError, match="once"):
        evaluate(tmp_path, "holdout")
    assert digest(tmp_path / "holdout-shadow.json") == first


def test_evidence_gate_rejects_exposed_sessions_and_unsafe_controls():
    rows = [
        event(truth, score=0.9 if truth == "self" else 0.1)
        | {"split": split, "strict_independent": True}
        for split in ("development", "holdout")
        for truth in ("self", "non-self")
    ]
    comparison = {
        "holdout": {
            rule: {
                "all": {"negative_self": 0},
                "2-4": {"self_accepted": int(rule != "current_two_window")},
            }
            for rule in RULES
        }
    }
    regression = {
        "metrics": {
            "paired": {"negative_self": 0},
            "production_ownership": {"negative_self": 0},
        }
    }
    assert (
        evidence_status(rows, comparison, regression, PARAMS)[0]
        == "PROMISING FOR SHADOW"
    )
    rows[2].update(event("self", score=0.1))
    assert (
        evidence_status(rows, comparison, regression, PARAMS)[0]
        == "NO SAFE SHORT-SPEECH RULE FOUND"
    )
    rows[2].update(event("self", score=0.9))
    for rule in RULES:
        if rule != "current_two_window":
            comparison["holdout"][rule]["all"]["negative_self"] = 1
    assert (
        evidence_status(rows, comparison, regression, PARAMS)[0]
        == "NO SAFE SHORT-SPEECH RULE FOUND"
    )
    rows[0]["strict_independent"] = False
    assert (
        evidence_status(rows, comparison, regression, PARAMS)[0]
        == "INSUFFICIENT EVIDENCE"
    )


def test_real_readonly_scoring_cannot_change_identity_enrollment_or_blind(
    world, tmp_path, monkeypatch
):
    _, core, *_ = world
    session = seed(world)
    anchor(core)
    e = event() | {
        "utterance_id": fixtures._utterance_id(1),
        "media_id": "media-1",
        "storage_key": "fixture/1",
        "source_start_ms": 1000,
        "source_end_ms": 4040,
        "session_id": session,
        "split": "development",
    }
    manifest(tmp_path, [e])
    with core.database.read() as c:
        before = integrity(c)
    write(tmp_path / "before/fingerprints.json", before)
    monkeypatch.setattr(
        "short_self_evidence.FunASRSpeakerEmbeddingProvider",
        lambda *a, **kw: Provider(),
    )
    monkeypatch.setattr("short_self_evidence.FunASRBackend", lambda *a, **kw: object())
    result = score_partition(core.paths.state_dir, tmp_path, "development")
    assert result["events"] == 1
    data = json.loads((tmp_path / "development-evidence.json").read_text())["events"][0]
    assert data["full"]["decision"] == "self"
    assert data["current_two_window"] == "unknown"
    with core.database.read() as c:
        assert integrity(c) == before
    c = readonly(core.paths.state_dir)
    with pytest.raises(Exception, match="readonly"):
        c.execute("UPDATE utterances SET identity='self'")
    c.close()
    malformed = copy.deepcopy(e)
    malformed["source_end_ms"] = 11000
    other = tmp_path / "invalid"
    other.mkdir()
    manifest(other, [malformed])
    write(other / "before/fingerprints.json", before)
    with pytest.raises(ValueError, match="malformed"):
        score_partition(core.paths.state_dir, other, "development")
    assert not (other / "development-evidence.json").exists()
