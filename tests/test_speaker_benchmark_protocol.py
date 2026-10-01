"""Freeze/gate semantics with synthetic audio; never a real model comparison."""

import hashlib
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from speaker_benchmark_protocol import (  # noqa: E402
    FORBIDDEN_FLAGS,
    evaluate_once,
    fit_and_freeze,
    independent_gate,
    trusted_event,
)
from audit_speaker_benchmark_readiness import bind_database_events  # noqa: E402
from tests.test_speaker_research_reservation import prospective, world as world  # noqa: E402
from tests import test_v34_open_speaker_identity as fixtures  # noqa: E402


def audio(e):
    n = e["duration_ms"] * 16
    return np.full(n, 1 if e["truth"] == "self" else 2, np.float32), n


def event(i, role="independent_evaluation", duration=2500):
    date = f"2030-01-{1 + i % 3:02}"
    if role == "development":
        date = "2030-02-01"
    e = {
        "event_id": f"{role}-{i}",
        "utterance_id": f"u-{role}-{i}",
        "session_id": role + date,
        "date": date,
        "session_role": role,
        "duration_ms": duration,
        "truth": "self" if i % 2 == 0 else "non-self",
        "reservation": {
            "research_role": role,
            "capture_date_utc": date,
            "reserved_at": "2029-01-01T00:00:00Z",
            "first_prediction_at": "2029-01-01T00:00:01Z",
        },
        "truth_reviewed_at": "2029-01-01T00:00:02Z",
        "truth_fact_ids": [f"f-{i}"],
        "purity": "clean_single",
        "mapping_complete": True,
        "has_overlap": False,
        "exclusions": [],
        **dict.fromkeys(FORBIDDEN_FLAGS, False),
    }
    e["was_development"] = role == "development"
    e["source_windows"] = [
        {
            "media_id": e["event_id"],
            "storage_key": e["event_id"],
            "sha256": hashlib.sha256(e["event_id"].encode()).hexdigest(),
            "start_ms": 0,
            "end_ms": duration,
        }
    ]
    e["normalized_audio_sha256"] = hashlib.sha256(audio(e)[0].tobytes()).hexdigest()
    return e


def study():
    evaluation = [event(i, duration=2500 if i % 4 < 2 else 3500) for i in range(30)]
    evaluation += [event(30 + i, duration=5000 if i < 2 else 6500) for i in range(4)]
    return [event(i, "development") for i in range(2)], evaluation


class Backend:
    calls = 0

    def metadata(self):
        return {"model_hash": "synthetic-unit-test", "version": "1", "dimension": 2}

    def embed(self, wave, length):
        self.calls += 1
        assert length == len(wave)
        return np.array([1, 0] if wave[0] == 1 else [0, 1], np.float32)

    def embed_batch(self, inputs, *, batch_size):
        return np.array([self.embed(a, n) for a, n in inputs])


def models(backend):
    return {
        "synthetic": (
            backend,
            np.array([[1, 0]], np.float32),
            np.array([1, 0], np.float32),
        )
    }


def test_real_independent_minimum_gate_and_no_inference_when_absent(tmp_path):
    dev, held = study()
    assert independent_gate(held)["passed"]
    backend = Backend()
    with pytest.raises(ValueError, match="INDEPENDENT DATA GATE NOT MET"):
        fit_and_freeze(tmp_path, dev, [], models(backend), audio)
    assert backend.calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("was_enrollment", True),
        ("was_calibration", None),
        ("was_blind", True),
        ("exclusions", ["overlap"]),
        ("mapping_complete", False),
    ],
)
def test_contamination_missing_provenance_and_exclusions_fail_gate(field, value):
    _, held = study()
    held[0][field] = value
    assert not independent_gate(held)["passed"]


def test_timestamp_and_source_mapping_are_not_self_declared_strings():
    e = event(0)
    e["reservation"]["reserved_at"] = e["reservation"]["first_prediction_at"]
    assert not trusted_event(e, "independent_evaluation")
    e = event(0)
    e["reservation"]["reserved_at"] = "a"
    assert not trusted_event(e, "independent_evaluation")
    e = event(0)
    e["source_windows"][0]["end_ms"] += 1
    assert not trusted_event(e, "independent_evaluation")


def test_duplicate_source_and_same_date_split_are_rejected_before_model(tmp_path):
    dev, held = study()
    backend = Backend()
    held[1]["source_windows"] = held[0]["source_windows"]
    with pytest.raises(ValueError, match="overlapping source"):
        fit_and_freeze(tmp_path, dev, held, models(backend), audio)
    dev, held = study()
    for e in dev:
        e["date"] = held[0]["date"]
        e["reservation"]["capture_date_utc"] = e["date"]
    with pytest.raises(ValueError, match="session/date overlap"):
        fit_and_freeze(tmp_path, dev, held, models(backend), audio)
    assert backend.calls == 0


def test_companion_sensitive_model_never_reads_evaluation_audio(tmp_path):
    class Padding(Backend):
        def embed_batch(self, inputs, *, batch_size):
            return np.tile([0.6, 0.8], (len(inputs), 1))

    dev, held = study()
    backend = Padding()
    accessed = []

    def load(e):
        accessed.append(e["session_role"])
        return audio(e)

    plan = fit_and_freeze(tmp_path, dev, held, models(backend), load)
    assert (
        plan["models"]["synthetic"]["invariance"]["status"]
        == "ENGINEERING INVALID FOR BENCHMARK"
    )
    with pytest.raises(ValueError, match="ENGINEERING INVALID"):
        evaluate_once(tmp_path, models(backend), load)
    assert set(accessed) == {"development"}
    assert not (tmp_path / "evaluation-started.json").exists()


def test_frozen_study_metrics_and_evaluation_once(tmp_path):
    dev, held = study()
    backend = Backend()
    fit_and_freeze(tmp_path, dev, held, models(backend), audio)
    result = evaluate_once(tmp_path, models(backend), audio)
    assert result["production_enabled"] is False
    assert result["models"]["synthetic"]["all"]["self_accepted"] == 17
    assert result["models"]["synthetic"]["all"]["negative_self"] == 0
    before = (tmp_path / "evaluation.json").read_bytes()
    with pytest.raises(FileExistsError):
        evaluate_once(tmp_path, models(backend), audio)
    assert (tmp_path / "evaluation.json").read_bytes() == before
    with pytest.raises(ValueError, match="already frozen"):
        fit_and_freeze(tmp_path, dev, held, models(backend), audio)


def test_changed_truth_or_failed_audio_consumes_study(tmp_path):
    dev, held = study()
    backend = Backend()
    fit_and_freeze(tmp_path, dev, held, models(backend), audio)
    path = tmp_path / "evaluation-manifest.json"
    original = path.read_text()
    path.write_text(original.replace('"truth": "self"', '"truth": "non-self"', 1))
    with pytest.raises(ValueError, match="source/truth/split changed"):
        evaluate_once(tmp_path, models(backend), audio)
    path.write_text(original)
    with pytest.raises(ValueError, match="source/preprocessing"):
        evaluate_once(
            tmp_path, models(backend), lambda e: (audio(e)[0] * 3, audio(e)[1])
        )
    with pytest.raises(FileExistsError):
        evaluate_once(tmp_path, models(backend), audio)


def test_database_binding_rejects_fabricated_independent_role(world):
    _, core, *_ = world
    prospective(world, role="development")
    e = event(0)
    e["utterance_id"] = fixtures._utterance_id(1)
    with (
        core.database.read() as c,
        pytest.raises(ValueError, match="provenance differs"),
    ):
        bind_database_events(c, [e])


def test_database_binding_checks_actual_review_source_and_purity(world):
    from allday_asr.v3.adapters.sqlite.speaker_research_reservations import (
        provenance,
        record_prediction,
    )

    _, core, *_ = world
    sid, _ = prospective(world)
    uid = fixtures._utterance_id(1)
    with core.database.transaction() as c:
        record_prediction(c, sid)
    core.people.assign_utterances(
        [{"utterance_id": uid, "revision": 1}],
        person_id=world[4],
        display_name=None,
        actor="test-human-review",
    )
    with core.database.transaction() as c:
        f = c.execute(
            "SELECT * FROM annotation_facts WHERE source_utterance_id=? AND actor='test-human-review'",
            (uid,),
        ).fetchone()
        a = c.execute(
            "SELECT * FROM annotation_fact_audio WHERE fact_id=? LIMIT 1",
            (f["fact_id"],),
        ).fetchone()
        source = c.execute(
            "SELECT a.sha256,r.storage_key FROM audio_assets a JOIN capture_segments s USING(asset_id) JOIN audio_replicas r USING(replica_id) WHERE a.media_id=? AND s.session_id=?",
            (a["media_id"], sid),
        ).fetchone()
        e = event(0)
        e.update(provenance(c, uid))
        e.update(
            utterance_id=uid,
            date=e["reservation"]["capture_date_utc"],
            truth="non-self",
            truth_fact_ids=[f["fact_id"]],
            truth_reviewed_at=f["created_at"],
        )
        e["source_windows"] = [
            {
                "media_id": a["media_id"],
                "sha256": source[0],
                "storage_key": source[1],
                "start_ms": a["start_ms"],
                "end_ms": a["end_ms"],
            }
        ]
        with pytest.raises(ValueError, match="clean-single"):
            bind_database_events(c, [e])
        c.execute(
            "INSERT INTO speaker_purity_sources VALUES('bench-source',?,?,?)",
            (a["media_id"], a["start_ms"], a["end_ms"]),
        )
        c.execute(
            "INSERT INTO speaker_source_purity_evidence VALUES('bench-purity','bench-source','clean_single',?,0,'[]','{}',?)",
            (world[4], f["created_at"]),
        )
        c.execute(
            "INSERT INTO speaker_purity_current VALUES('bench-source','bench-purity')"
        )
        assert bind_database_events(c, [e]) == [e]
        e["source_windows"][0]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="source mapping"):
            bind_database_events(c, [e])
