import json
import sqlite3
import hashlib
from dataclasses import replace

import pytest
from tests.test_blind_validation import seed
from allday_asr.v3.application.blind_scoring import digest, encoded
from allday_asr.v3.domain.speaker_turns import PROJECTION_VERSION, QUERY_BUILDER_VERSION
from allday_asr.v3.adapters.sqlite.blind_queries import automatic_queries, latest_run
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork

pytest_plugins = ["tests.test_blind_validation"]


def v2_task(world):
    _, core, service, *_ = world
    seed(world)
    assert service.run_once()
    task = service.tasks()[0]
    service.upgrade_turn_experiment("synthetic-v2")
    # Fixture creates an immutable V2 task by insert; never mutates real V1 truth/query.
    with core.database.transaction() as c:
        q = json.loads(c.execute("SELECT query_json FROM blind_query_views").fetchone()[0])
        q.update(query_id="query-v2", review_schema_version=2, model_window_order=[0,1],
                 review_window_order=[1,0], projection_version=PROJECTION_VERSION)
        c.execute("INSERT INTO blind_query_views SELECT ?,session_id,speaker_track_id,'synthetic-v2',?,?,created_at "
                  "FROM blind_query_views LIMIT 1", ("query-v2",encoded(q),digest(q)))
        c.execute("INSERT INTO blind_prediction_snapshots SELECT 'pred-v2',?,'synthetic-v2',prediction_json,prediction_created_at "
                  "FROM blind_prediction_snapshots LIMIT 1", ("query-v2",))
        c.execute("INSERT INTO blind_events SELECT 'event-v2','synthetic-v2',session_id,'query-v2','[\"query-v2\"]','{}',created_at,NULL FROM blind_events LIMIT 1")
        c.execute("INSERT INTO blind_review_tasks SELECT 'task-v2','event-v2',?,priority,created_at "
                  "FROM blind_review_tasks LIMIT 1", ("query-v2",))
    return task


@pytest.mark.parametrize("composition,boundary", [
    ("clean_single","clean"),("simultaneous_overlap","clean"),("sequential_multi_speaker","clean"),
    ("backchannel","clean"),("clean_single","cut"),("uncertain","uncertain")])
def test_v2_submit_edit_undo_replay_and_same_audio_set(world,composition,boundary):
    legacy = v2_task(world)
    _, core, service, _, alpha, *_ = world
    task = next(t for t in service.tasks() if t["source_id"] == "task-v2")
    assert task["context"]["review_schema_version"] == 2
    clips = task["context"]["voice_candidates"][0]["representative_clips"]
    with core.database.read() as c:
        q = json.loads(c.execute("SELECT query_json FROM blind_query_views WHERE query_id='query-v2'").fetchone()[0])
    assert [(w["media_id"],w["start_ms"],w["end_ms"]) for w in clips] == [
        (q["windows"][i]["media_id"],q["windows"][i]["start_ms"],q["windows"][i]["end_ms"]) for i in [1,0]]
    assert next(t for t in service.tasks() if t["source_id"] == legacy["source_id"])["context"]["review_schema_version"] == 1
    payload = dict(action="submit",operation_id="v2-first",primary_speaker_person_id=alpha,
                   review_schema_version=2,speaker_composition=composition,boundary_quality=boundary)
    first = service.submit("task-v2",payload,"test")
    assert first["purity"] is None and first["speaker_composition"] == composition
    assert service.submit("task-v2",payload,"test") == first
    edited = service.submit("task-v2",payload | {"operation_id":"v2-edit","boundary_quality":"cut"},"test")
    assert edited["revision"] == 2 and edited["boundary_quality"] == "cut"
    report = service.report()
    assert report["speaker_composition_strata"][composition]["legacy_G"]["known"] == 1
    service.submit("task-v2",dict(action="undo",operation_id="v2-undo"),"test")
    assert any(t["source_id"] == "task-v2" for t in service.tasks())
    with pytest.raises(ValueError,match="schema"):
        service.submit("task-v2",dict(action="submit",purity="mixed_overlap",primary_speaker_person_id=alpha),"test")
    with core.database.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError,match="immutable"):
            c.execute("UPDATE blind_ground_truth SET speaker_composition='uncertain'")


def test_freeze_clones_model_matcher_profiles_and_excludes_all_old_sessions(world):
    _, core, service, _, _, _, _, snapshot = world
    session = seed(world)
    assert service.run_once()
    before = service.report("synthetic-v1")["progress"]["generated_events"]
    v2 = service.upgrade_turn_experiment("synthetic-v2")
    frozen = json.loads(v2["snapshot_json"])
    for key in ("model","model_files","model_sha256","legacy","clean","profile_hashes","matcher",
                "matcher_version","GP_version","query_probe_thresholds"):
        assert frozen[key] == snapshot[key]
    assert frozen["query_builder_version"] == QUERY_BUILDER_VERSION
    assert frozen["review_schema_version"] == 2
    assert service.upgrade_turn_experiment("synthetic-v2")["snapshot_hash"] == v2["snapshot_hash"]
    assert not service.enqueue(session)
    assert service.report("synthetic-v2")["progress"]["generated_events"] == 0
    assert service.report("synthetic-v1")["progress"]["generated_events"] == before
    with core.database.read() as c:
        assert c.execute("SELECT retired_at FROM blind_experiments WHERE experiment_id='synthetic-v1'").fetchone()[0]
        assert set(frozen["seen_audio_sha256"]) == {r[0] for r in c.execute("SELECT sha256 FROM audio_assets")}


def test_new_v2_session_builder_prediction_review_identity_and_isolation(world):
    helper, core, service, *_ = world
    old = seed(world)
    service.upgrade_turn_experiment("synthetic-v2")
    new = seed(world, number=2)
    assert not service.enqueue(old)
    with SqliteUnitOfWork(core.database) as uow:
        row = uow.desktop.connection.execute("SELECT utterance_id FROM utterances WHERE session_id=? AND ordinal=0",(new,)).fetchone()
        template = uow.evidence.get_utterance(row[0])
        for i,start in enumerate((2000,4000,6000,8000),2):
            uow.evidence.add_utterance(replace(template,utterance_id="v2-fixture-"+str(i),
                ordinal=i,start_ms=start,end_ms=start+1000))
    with core.database.transaction() as c:
        run = latest_run(c,new)
        label = c.execute("SELECT label FROM speaker_tracks WHERE run_id=?",(run,)).fetchone()[0]
        # Synthetic persisted evidence exercises the actual builder/service handoff.
        evidence = {
            "v3_asr_evidence": dict(primary_tokens=[dict(start_ms=i,end_ms=i+1000,text="fixture")
                                                    for i in range(0,60000,1000)],hypotheses=[]),
            "v3_diarization_evidence": dict(exclusive_turns=[
                dict(start_ms=0,end_ms=60000,speaker_label=label)],regular_turns=[]),
            "v3_transcript_evidence": dict(projection_version=PROJECTION_VERSION),
        }
        root = helper.root / "artifacts"
        root.mkdir(parents=True,exist_ok=True)
        for kind,data in evidence.items():
            raw = encoded(data).encode()
            (root/kind).write_bytes(raw)
            c.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (kind,run,kind,"fixture","2","digest","[]",kind,hashlib.sha256(raw).hexdigest(),
                 len(raw),"active","{}",None,"2026-01-01"))
        c.execute("UPDATE utterances SET start_ms=0,end_ms=1000 WHERE session_id=? AND ordinal=0",(new,))
        queries = automatic_queries(c,new,"synthetic-v2",run,builder_version=QUERY_BUILDER_VERSION,artifact_root=root)
        assert queries == automatic_queries(c,new,"synthetic-v2",run,builder_version=QUERY_BUILDER_VERSION,artifact_root=root)
        assert queries[0]["window_count"] == 5 and queries[0]["duration_s"] <= 40
        assert any(r["reason"] == "explicit_query_window_budget" for r in queries[0]["unselected_windows"])
    assert service.run_once()
    task = next(t for t in service.tasks() if t["session_id"] == new)
    assert task["context"]["review_schema_version"] == 2
    with core.database.read() as c:
        assert c.execute("SELECT COUNT(*) FROM voice_prototypes").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM annotation_sample_queue").fetchone()[0] == 0
        q = json.loads(c.execute("SELECT query_json FROM blind_query_views WHERE session_id=?",(new,)).fetchone()[0])
    clips = task["context"]["voice_candidates"][0]["representative_clips"]
    assert sorted((w["media_id"],w["start_ms"],w["end_ms"]) for w in q["windows"]) == sorted(
        (w["media_id"],w["start_ms"],w["end_ms"]) for w in clips)
