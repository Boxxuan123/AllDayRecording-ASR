"""Synthetic public tests; private integration artifacts are explicitly opt-in."""

import json
import os
import sqlite3
import shutil
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import numpy as np
import pytest

from allday_asr.v3.adapters.sqlite.migrations.v021_speaker_profile_purity import (
    SQL as AUDIT_SQL,
)
from allday_asr.v3.adapters.sqlite.migrations.v022_enrollment_purity import SQL
from allday_asr.v3.adapters.sqlite.speaker_purity_repository import (
    current_evidence,
    ensure_source,
    propose_recrop,
    reconcile,
    register_candidates,
)
from allday_asr.v3.adapters.sqlite.annotation_sample_plan import SamplePlan
from allday_asr.v3.application.speaker_profile_purity import append_review, source_key
from allday_asr.v3.application.purity_shadow import build_shadow
from allday_asr.v3.application.query_purity import (
    fit_clean_thresholds,
    grouped_validation,
    probe,
    query_features,
)
from allday_asr.v3.domain.people import RepresentativeClip, SpeakerEmbedding
from allday_asr.v3.domain.speaker_purity import (
    PurityVerdict,
    SourcePurityEvidence,
    is_source_eligible_for_clean_profile,
)
from allday_asr.v3.ports.speaker_embeddings import SpeakerTrackInput


def connection():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("CREATE TABLE persons(person_id TEXT PRIMARY KEY, display_name TEXT)")
    c.executemany("INSERT INTO persons VALUES(?,?)", [("target", "T"), ("other", "O")])
    # Reconciliation must also handle duplicates imported from historical/future task archives.
    c.executescript(
        AUDIT_SQL.replace("source_key TEXT NOT NULL UNIQUE", "source_key TEXT NOT NULL")
    )
    c.executescript(SQL)
    c.execute(
        "INSERT INTO speaker_profile_purity_runs VALUES(?,?,?,?,?,?,?,?)",
        ("run", "v1", "CAM++", "v1", "embedding", "profile", "{}", "now"),
    )
    return c


def task(c, name="task", *, start=0, target="target"):
    c.execute(
        "INSERT INTO speaker_profile_purity_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            name,
            "run",
            source_key("media", start, start + 8000),
            target,
            "media",
            start,
            start + 8000,
            "session",
            "track",
            "cluster",
            "P0",
            "[]",
            "{}",
            "now",
        ),
    )


def review(c, name="task", *, purity="clean_single", person="target", action="submit"):
    return append_review(
        c,
        name,
        {
            "action": action,
            "primary_speaker_person_id": person,
            "primary_speaker_unknown": person is None,
            "purity": purity,
            "other_speaker_ids": [],
            "quality_flags": [],
        },
        "test",
    )


@pytest.mark.parametrize("verdict", list(PurityVerdict))
def test_only_reviewed_clean_matching_primary_is_eligible(verdict):
    e = SourcePurityEvidence("source", verdict, "target", "evidence")
    assert is_source_eligible_for_clean_profile(e, "target", audio_available=True) == (
        verdict == "clean_single"
    )
    assert not is_source_eligible_for_clean_profile(e, "other", audio_available=True)
    assert not is_source_eligible_for_clean_profile(e, "target", audio_available=False)
    assert not is_source_eligible_for_clean_profile(
        replace(e, conflicting=True), "target", audio_available=True
    )


def test_latest_revision_idempotent_reconciliation_and_undo_invalidate_grant():
    c = connection()
    task(c)
    review(c, purity="mixed_overlap")
    second = review(c)
    first = reconcile(c, "now")
    assert first["clean"] == 1
    assert first["sources"][0]["review_references"][0]["review_revision"] == 2
    assert (
        first["sources"][0]["review_references"][0]["review_id"] == second["review_id"]
    )
    assert reconcile(c, "later") == first
    assert (
        c.execute("SELECT COUNT(*) FROM speaker_source_purity_evidence").fetchone()[0]
        == 1
    )
    review(c, action="undo")
    assert current_evidence(c, source_key("media", 0, 8000)).verdict == "unreviewed"
    assert reconcile(c, "later")["unresolved"] == 1
    assert (
        c.execute("SELECT COUNT(*) FROM speaker_profile_purity_reviews").fetchone()[0]
        == 3
    )
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        c.execute("UPDATE speaker_source_purity_evidence SET purity='clean_single'")


def test_duplicate_range_shares_evidence_and_conflict_does_not_pick_newest():
    c = connection()
    task(c)
    task(c, "duplicate")
    review(c)
    review(c, "duplicate")
    summary = reconcile(c, "now")
    assert (
        summary["unique_sources"],
        summary["duplicate_tasks"],
        summary["clean"],
    ) == (1, 1, 1)
    review(c, "duplicate", purity="mixed_overlap")
    summary = reconcile(c, "later")
    assert summary["conflicting"] == 1 and summary["clean"] == 0
    assert current_evidence(c, source_key("media", 0, 8000)).conflicting


def test_new_duplicate_task_invalidates_existing_grant_until_reconciled():
    c = connection()
    task(c)
    review(c)
    reconcile(c, "now")
    task(c, "new-task")
    review(c, "new-task", purity="mixed_overlap")
    assert current_evidence(c, source_key("media", 0, 8000)).verdict == "unreviewed"
    assert reconcile(c, "later")["conflicting"] == 1


def test_target_associations_share_original_purity_but_wrong_is_ineligible():
    c = connection()
    task(c)
    task(c, "other-target", target="other")
    review(c)
    review(c, "other-target")
    reconcile(c, "now")
    assert c.execute("SELECT COUNT(*) FROM speaker_purity_sources").fetchone()[0] == 1
    e = current_evidence(c, source_key("media", 0, 8000))
    assert is_source_eligible_for_clean_profile(e, "target", audio_available=True)
    assert not is_source_eligible_for_clean_profile(e, "other", audio_available=True)


def test_reconciliation_rollback_preserves_all_old_data():
    c = connection()
    task(c)
    review(c)
    c.commit()
    with pytest.raises(RuntimeError), c:
        reconcile(c, "now")
        raise RuntimeError("simulated crash before commit")
    assert c.execute("SELECT COUNT(*) FROM speaker_purity_sources").fetchone()[0] == 0
    assert (
        c.execute("SELECT COUNT(*) FROM speaker_profile_purity_reviews").fetchone()[0]
        == 1
    )


def test_recrop_new_range_never_inherits_clean_and_must_be_contained():
    c = connection()
    parent = ensure_source(c, "media", 0, 8000)
    key = propose_recrop(c, parent, "target", "media", 0, 6000, "now", {})
    assert key != parent
    assert current_evidence(c, key) is None
    assert (
        c.execute("SELECT status FROM purity_candidates").fetchone()[0] == "unreviewed"
    )
    with pytest.raises(ValueError, match="contained"):
        propose_recrop(c, parent, "target", "media", 0, 9000, "now", {})


class RawOnlyProvider:
    model, model_version = "CAM++", "fixed"

    def __init__(self):
        self.seen = []

    def embed(self, tracks):
        self.seen.extend(tracks)
        assert all(c.storage_key == "raw-audio" for t in tracks for c in t.clips)
        return tuple(
            SpeakerEmbedding(
                t.speaker_track_id,
                self.model,
                self.model_version,
                (1.0, 0.0),
                tuple(
                    RepresentativeClip(c.media_id, c.source_start_ms, c.source_end_ms)
                    for c in t.clips
                ),
                1.0,
            )
            for t in tracks
        )


def enrollment_source(verdict="clean_single", *, start=0, end=8000, session="session"):
    key = source_key("media", start, end)
    return {
        "source_key": key,
        "target_person_id": "target",
        "source_session_id": session,
        "source_media_id": "media",
        "start_ms": start,
        "end_ms": end,
        "storage_key": "raw-audio",
        "audio_available": True,
        "evidence": SourcePurityEvidence(
            key, PurityVerdict(verdict), "target", "evidence"
        ),
        "legacy_embedding": (-1.0, 0.0),
    }


def test_shadow_reads_only_clean_raw_ranges_and_keeps_provenance():
    provider = RawOnlyProvider()
    rows = [enrollment_source()] + [
        enrollment_source(v, start=i * 8000, end=(i + 1) * 8000)
        for i, v in enumerate(
            (
                "mixed_overlap",
                "boundary_cross",
                "wrong_primary",
                "uncertain",
                "unreviewed",
            ),
            1,
        )
    ]
    centers = build_shadow(rows + rows, provider)
    assert len(centers) == 1 and len(provider.seen) == 1
    assert np.allclose(centers[0]["vector"], (1, 0))
    assert centers[0]["source_keys"] == [rows[0]["source_key"]]
    assert centers[0]["evidence_ids"] == ["evidence"]


def test_insufficient_shadow_does_not_fall_back_to_mixed():
    assert not build_shadow(
        [enrollment_source(end=1000), enrollment_source("mixed_overlap")],
        RawOnlyProvider(),
    )


def test_shadow_provider_cannot_substitute_unreviewed_range():
    class WrongRange(RawOnlyProvider):
        def embed(self, tracks):
            result = super().embed(tracks)
            return (
                replace(
                    result[0], representatives=(RepresentativeClip("media", 0, 9000),)
                ),
            )

    with pytest.raises(ValueError, match="differs"):
        build_shadow([enrollment_source()], WrongRange())


def test_missing_session_is_insufficient_for_session_profile():
    provider = RawOnlyProvider()
    assert not build_shadow([enrollment_source(session=None)], provider)
    assert not provider.seen


def test_version_21_to_22_is_additive_repeatable_and_preserves_audit():
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner

    base = Path(__file__).resolve().parents[1] / "outputs/test-purity"
    path = base / uuid4().hex / "core.sqlite3"
    try:
        assert (
            V3MigrationRunner(
                path, migrations=[m for m in MIGRATIONS if m.version <= 21]
            ).initialize()
            == 21
        )
        with closing(sqlite3.connect(path)) as c, c:
            c.execute(
                "INSERT INTO speaker_profile_purity_runs VALUES(?,?,?,?,?,?,?,?)",
                ("run", "v1", "model", "version", "hash", "profile", "{}", "now"),
            )
        assert V3MigrationRunner(path).initialize() == 22
        assert V3MigrationRunner(path).initialize() == 22
        with closing(sqlite3.connect(path)) as c:
            assert (
                c.execute(
                    "SELECT audit_run_id FROM speaker_profile_purity_runs"
                ).fetchone()[0]
                == "run"
            )
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        assert path.parent.resolve().is_relative_to(base.resolve())
        if path.parent.exists():
            shutil.rmtree(path.parent)


def test_worker_candidate_queue_is_bounded_and_deduplicated():
    c = connection()
    windows = tuple(
        {"media_id": "media", "start_ms": i * 8000, "end_ms": (i + 1) * 8000}
        for i in range(10)
    )
    p = SamplePlan(
        "sample",
        "target",
        "cluster",
        SpeakerTrackInput("track", "session", ()),
        ("fact",),
        windows,
    )
    register_candidates(c, p, "now")
    register_candidates(c, p, "later")
    rows = c.execute(
        "SELECT status,COUNT(*) FROM purity_candidates GROUP BY status"
    ).fetchall()
    assert dict(rows) == {"not_needed": 5, "unreviewed": 5}


def feature(vectors):
    return query_features(
        [
            {
                "media_id": "media",
                "start_ms": i * 2000,
                "end_ms": (i + 1) * 2000,
                "embedding": v,
            }
            for i, v in enumerate(vectors)
        ]
    )


def test_probe_consistent_mixed_insufficient_without_any_identity():
    clean = feature([(1, 0), (1, 0), (1, 0)])
    thresholds = fit_clean_thresholds([{"gold": "clean_single", "features": clean}])
    assert probe(clean, thresholds)["status"] == "PASS"
    assert (
        probe(feature([(1, 0), (0, 1), (1, 0)]), thresholds)["status"] == "SUSPICIOUS"
    )
    assert probe(feature([(1, 0)]), thresholds)["status"] == "INSUFFICIENT"
    assert probe(feature([(1, 0), (-1, 0), (0, 1)]), {})["status"] == "INSUFFICIENT"
    assert clean["total_speech_duration_s"] is None
    assert clean["speaker_track_consistent"] is None


def test_grouped_thresholds_never_train_on_held_out_source_component():
    rows = [
        {
            "source_key": str(i),
            "group": g,
            "gold": "clean_single",
            "features": feature(v),
        }
        for i, (g, v) in enumerate(
            [
                ("one", [(1, 0)] * 3),
                ("one", [(1, 0)] * 3),
                ("two", [(1, 0), (0.7, 0.7), (1, 0)]),
            ]
        )
    ]
    result = grouped_validation(rows)
    assert result["groups"] == 2
    assert len(result["outcomes"]["min_pairwise_cosine"]) == 3
    assert all(f["held_out_group"] not in f["train_groups"] for f in result["folds"])
    assert result["folds"][0]["thresholds"] != result["folds"][1]["thresholds"]


@pytest.mark.skipif(
    os.environ.get("ALLDAY_PRIVATE_PURITY") != "1",
    reason="private local integration opt-in",
)
def test_private_historical_event_regression():
    path = (
        Path(__file__).resolve().parents[1]
        / "outputs/enrollment-purity-gate-20260930/historical-regression.json"
    )
    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["independent_event_count"] == 1
    assert result["correlated_query_views"] == 3
    assert result["regression_passed"] and all(result["assertions"].values())
