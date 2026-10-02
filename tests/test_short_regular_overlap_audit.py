"""Acoustic audit geometry and isolated review writes; never relax production gates."""

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import audit_short_regular_overlap as audit
import build_overlap_review_manifest as review
from short_overlap_geometry import (
    analyze,
    bucket,
    intersection,
    pattern_cells,
    sensitivity,
    transitions,
)
from short_self_dataset import digest, readonly, write


def turn(speaker, lo, hi):
    return {"speaker_label": speaker, "start_ms": lo, "end_ms": hi}


@pytest.mark.parametrize(
    "span,expected",
    [
        ((1000, 1075), "START_EDGE"),
        ((8925, 9000), "END_EDGE"),
        ((4000, 4075), "INTERIOR"),
        ((1000, 9000), "FULL"),
    ],
)
def test_owned_target_regular_foreign_position(span, expected):
    result = analyze(
        1000,
        9000,
        "target",
        [turn("target", 900, 9100), turn("foreign", *span)],
        [turn("target", 900, 9100)],
    )
    assert result["b_pattern"]
    assert result["intersections"][0]["position"] == expected


def test_case_b_geometry_preserves_75ms_and_152ms_transition():
    regular = [turn("foreign", 467, 1075), turn("target", 923, 12870)]
    exclusive = [turn("foreign", 467, 923), turn("target", 923, 12870)]
    result = analyze(1000, 9000, "target", regular, exclusive)
    assert result["full_target_exclusive_ownership"] and result["edge_b_pattern"]
    assert result["intersection_ms"] == 75
    assert result["foreign_exclusive_ranges"] == []
    assert transitions(regular, exclusive)[0]["foreign_tail_after_switch_ms"] == 152


@pytest.mark.parametrize("foreign", [(0, 1000), (9000, 9500)])
def test_adjacent_half_open_intervals_are_not_overlap(foreign):
    result = analyze(
        1000,
        9000,
        "t",
        [turn("t", 1000, 9000), turn("f", *foreign)],
        [turn("t", 1000, 9000)],
    )
    assert not result["b_pattern"] and not result["intersections"]


def test_one_millisecond_simultaneous_activity_is_preserved():
    result = analyze(
        1000,
        9000,
        "t",
        [turn("t", 1000, 9000), turn("f", 1000, 1001)],
        [turn("t", 1000, 9000)],
    )
    assert result["intersection_ms"] == 1
    assert result["intersections"][0]["bucket"] == "0-25"


def test_regular_foreign_without_target_activity_is_not_simultaneous():
    result = analyze(
        1000,
        9000,
        "t",
        [turn("t", 2000, 9000), turn("f", 1000, 1075)],
        [turn("t", 1000, 9000)],
    )
    assert result["foreign_regular_ranges"] == [[1000, 1075]]
    assert not result["b_pattern"]


def test_foreign_exclusive_and_partial_ownership_are_not_strict_b_pattern():
    r = [turn("t", 1000, 9000), turn("f", 1000, 1075)]
    assert not analyze(
        1000, 9000, "t", r, [turn("t", 1075, 9000), turn("f", 1000, 1075)]
    )["b_pattern"]
    assert not analyze(1000, 9000, "t", r, [turn("t", 1075, 9000)])["b_pattern"]


def test_duplicate_foreign_turns_do_not_double_count_duration():
    result = analyze(
        1000,
        9000,
        "t",
        [turn("t", 1000, 9000), turn("f", 1000, 1075), turn("f", 1000, 1075)],
        [turn("t", 1000, 9000)],
    )
    assert result["intersection_ms"] == 75 and len(result["intersections"]) == 1


@pytest.mark.parametrize(
    "ms,label",
    [
        (1, "0-25"),
        (25, "0-25"),
        (26, "25-50"),
        (50, "25-50"),
        (75, "50-100"),
        (100, "50-100"),
        (101, "100-200"),
        (200, "100-200"),
        (201, "200-500"),
        (500, "200-500"),
        (501, "500+"),
    ],
)
def test_duration_buckets_are_statistics_only(ms, label):
    assert bucket(ms) == label


def test_sensitivity_is_a_copy_and_does_not_change_frozen_evidence():
    event = {
        "range_ms": [1000, 9000],
        "target_label": "t",
        "regular_turns": [turn("t", 923, 12870), turn("f", 467, 1075)],
        "exclusive_turns": [turn("t", 923, 12870), turn("f", 467, 923)],
    }
    before = json.dumps(event, sort_keys=True)
    rows = sensitivity(event)
    assert [r["geometry"]["intersection_ms"] for r in rows] == [
        152,
        125,
        100,
        75,
        50,
        25,
        0,
    ]
    assert before == json.dumps(event, sort_keys=True)


def test_pattern_cells_partition_time_and_do_not_equate_exclusive_with_no_overlap():
    r = [turn("t", 1000, 9000), turn("f", 1000, 1075)]
    assert pattern_cells(1000, 9000, "t", r, [turn("t", 1000, 9000)]) == {
        "regular_target+foreign/exclusive_target": 75,
        "regular_target_only/exclusive_target": 7925,
    }
    assert intersection([0, 1000], [1000, 2000]) is None


@pytest.fixture
def queue(tmp_path):
    output = tmp_path / "audit"
    output.mkdir()
    write(
        output / "manifest.json",
        {
            "format": "short-regular-overlap-acoustic-audit-v1",
            "strict_human_events": [{"event_id": "anchor", "anchor": True}],
        },
    )
    (output / "manifest.sha256").write_text(
        digest(output / "manifest.json"), encoding="ascii"
    )
    audio = output / "test.wav"
    sf.write(audio, np.ones(1200, dtype=np.float32) * 0.01, 16000)
    clips = {
        name: {
            "path": str(audio),
            "sha256": digest(audio),
            "requested_range_ms": [0, 75],
        }
        for name in ["full-context", "target-boundary", "overlap-only"]
    }
    write(
        output / "audio-index.json",
        [
            {
                "event_id": "anchor",
                "component_index": 0,
                "component": {"start_ms": 0, "end_ms": 75, "duration_ms": 75},
                "clips": clips,
            }
        ],
    )
    review.build(output)
    return output


def test_review_writes_only_independent_ledger_and_no_identity_profile_or_artifact(
    queue, tmp_path, monkeypatch
):
    production = tmp_path / "production"
    production.mkdir()
    for name in ["core.sqlite3", "profile.json", "artifact.json", "model.bin"]:
        (production / name).write_bytes(b"frozen production sentinel")
    before = {str(p): digest(p) for p in production.iterdir()}
    frozen = digest(queue / "manifest.json")

    def forbidden(*args, **kwargs):
        raise AssertionError("review opened a database")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    result = review.record(
        queue,
        "anchor",
        0,
        "uncertain",
        reviewer="synthetic-test",
        listened_context=True,
    )
    assert result["decision"] == "uncertain"
    assert (
        len(json.loads((queue / "overlap-review-ledger.json").read_text())["reviews"])
        == 1
    )
    assert before == {str(p): digest(p) for p in production.iterdir()}
    assert frozen == digest(queue / "manifest.json")


@pytest.mark.parametrize(
    "decision,listened", [("self", True), ("target_only", False), ("uncertain", None)]
)
def test_review_rejects_identity_labels_and_unheard_results(queue, decision, listened):
    with pytest.raises(ValueError):
        review.record(
            queue, "anchor", 0, decision, reviewer="test", listened_context=listened
        )
    assert read_ledger(queue) == []


def read_ledger(queue):
    return json.loads((queue / "overlap-review-ledger.json").read_text())["reviews"]


def test_review_rejects_changed_audio_fingerprint(queue):
    (queue / "test.wav").write_bytes(b"changed")
    with pytest.raises(ValueError, match="fingerprint"):
        review.record(
            queue, "anchor", 0, "target_only", reviewer="test", listened_context=True
        )


def test_audit_database_connection_is_readonly(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    c = sqlite3.connect(state / "core.sqlite3")
    c.execute("CREATE TABLE protected(value)")
    c.execute("INSERT INTO protected VALUES ('unchanged')")
    c.commit()
    c.close()
    before = digest(state / "core.sqlite3")
    c = readonly(state)
    with pytest.raises(sqlite3.OperationalError):
        c.execute("UPDATE protected SET value='changed'")
    c.close()
    assert before == digest(state / "core.sqlite3")


def test_clip_keeps_sample_exact_overlap_and_unchanged_source(tmp_path):
    state = tmp_path / "state"
    (state / "audio").mkdir(parents=True)
    source = state / "audio" / "source.wav"
    sf.write(source, np.arange(160000, dtype=np.float32) / 320000, 16000)
    before = digest(source)
    cap = {
        "segment_id": "cap",
        "sequence": 0,
        "session_start_ms": 60000,
        "session_end_ms": 70000,
        "source_start_ms": 0,
        "source_end_ms": 10000,
        "storage_key": "source.wav",
        "sha256": before,
    }
    result = audit.clip(state, [cap], 61000, 61075, tmp_path / "overlap.wav")
    wave, rate = sf.read(result["path"])
    assert len(wave) == 1200 and rate == 16000 and digest(source) == before
    assert result["captures"][0]["source_range_ms"] == [1000, 1075]
    with pytest.raises(ValueError, match="incomplete"):
        audit.clip(state, [cap], 69950, 70050, tmp_path / "invalid.wav")
