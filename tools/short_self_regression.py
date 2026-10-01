"""Replay frozen diagnostic and actual ownership queries with production batching."""

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from short_self_dataset import digest, integrity, read, readonly, write
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.models.funasr import FunASRBackend
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite.product_self_queries import product_self_queries
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def replay(
    state, output, previous_manifest, previous_replay, *, verify_prior_baseline=True
):
    state, output = Path(state), Path(output)
    target = output / "fixed-regression.json"
    if target.exists():
        raise ValueError("regression already recorded")
    events = read(previous_manifest)["events"]
    previous = read(previous_replay)["events"]
    if len(events) != 24 or any(
        a["utterance_id"] != b["utterance_id"]
        for a, b in zip(events, previous, strict=True)
    ):
        raise ValueError("fixed cohort differs from prior query-coverage replay")
    c = readonly(state)
    clone = sqlite3.connect(":memory:")
    clone.row_factory = sqlite3.Row
    c.backup(clone)
    # Counterfactual projection only; real DB, audio facts and frozen truth never change.
    triggers = list(
        clone.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='utterances'"
        )
    )
    for name, _ddl in triggers:
        clone.execute('DROP TRIGGER "' + name + '"')
    for e in events:
        row = clone.execute(
            "SELECT evidence_json FROM utterances WHERE utterance_id=?",
            (e["utterance_id"],),
        ).fetchone()
        evidence = json.loads(row[0])
        evidence.pop("person_annotation", None)
        clone.execute(
            "UPDATE utterances SET speaker_track_id=original_speaker_track_id,evidence_json=? WHERE utterance_id=?",
            (json.dumps(evidence), e["utterance_id"]),
        )
    for _name, ddl in triggers:
        clone.execute(ddl)
    clone.commit()
    queries = {}
    for sid in dict.fromkeys(e["session_id"] for e in events):
        queries.update(
            {
                q["utterance_id"]: q
                for q in product_self_queries(clone, sid, state / "artifacts")
            }
        )
    tracks = []
    groups = {}
    full = []
    for i, e in enumerate(events):
        full.append(
            SpeakerTrackInput(
                str(i),
                e["session_id"],
                (
                    SpeakerClipInput(
                        e["media_id"],
                        e["storage_key"],
                        e["source_start_ms"],
                        e["source_end_ms"],
                        e["utterance_id"],
                    ),
                ),
            )
        )
        start, end = e["source_start_ms"], e["source_end_ms"]
        if previous[i]["actual_candidate_query"].get("replanning"):
            paired = queries[e["utterance_id"]]["tracks"]
        elif end - start >= 4000:
            mid = (start + end) // 2
            paired = tuple(
                SpeakerTrackInput(
                    f"paired:{i}:{j}",
                    e["session_id"],
                    (
                        SpeakerClipInput(
                            e["media_id"], e["storage_key"], a, b, e["utterance_id"]
                        ),
                    ),
                )
                for j, (a, b) in enumerate([(start, mid), (mid, end)])
            )
        else:
            paired = ()
        groups[i, "paired"] = paired
        groups[i, "production_ownership"] = queries[e["utterance_id"]]["tracks"]
    backend = FunASRBackend(device="cpu")
    provider = FunASRSpeakerEmbeddingProvider(
        ContentAddressedStore(state / "audio"),
        backend_factory=lambda: backend,
        temp_root=output / "regression-clips",
    )
    matcher = CalibratedSelfIdentityMatcher(state)
    # Exact old whole-clip ordering, default production batch=8: reproduce 3 false accepts.
    full_embeddings = {e.speaker_track_id: e for e in provider.embed(tuple(full))}
    raw = [
        dict(e)
        | {
            "current_single_default_batch": matcher.match(
                full_embeddings[str(i)]
            ).evidence
        }
        for i, e in enumerate(events)
    ]
    batch1 = {
        e["utterance_id"]: e
        for e in read(output / "reference-evidence.json")["events"]
        if e.get("previous_event_number")
    }
    contexts = [
        {
            "utterance_id": e["utterance_id"],
            "event_number": i + 1,
            "truth": e["truth"],
            "batch8_score": e["current_single_default_batch"].get("score"),
            "batch1_score": batch1[e["utterance_id"]]["full"].get("score"),
            "batch8_decision": e["current_single_default_batch"]["decision"],
            "batch1_decision": batch1[e["utterance_id"]]["full"]["decision"],
        }
        for i, e in enumerate(raw)
    ]
    rows = []
    for group in ["paired", "production_ownership"]:
        tracks = tuple(t for i in range(24) for t in groups[i, group])
        embeddings = {e.speaker_track_id: e for e in provider.embed(tracks)}
        for i, e in enumerate(events):
            windows = []
            for t in groups[i, group]:
                embedding = embeddings.get(t.speaker_track_id)
                windows.append(
                    {
                        "input": asdict(t),
                        "duration_ms": sum(
                            c.source_end_ms - c.source_start_ms for c in t.clips
                        ),
                        **(
                            matcher.match(embedding).evidence
                            if embedding
                            else {
                                "decision": "unknown",
                                "reason": "embedding_unavailable",
                            }
                        ),
                    }
                )
            decision = (
                "self"
                if len(windows) >= 2
                and all(
                    w["duration_ms"] >= 2000 and w["decision"] == "self"
                    for w in windows
                )
                else "unknown"
            )
            rows.append(
                {
                    "utterance_id": e["utterance_id"],
                    "truth": e["truth"],
                    "scope": group,
                    "decision": decision,
                    "windows": windows,
                }
            )

    def count(scope, truth, decision):
        return sum(
            r["scope"] == scope and r["truth"] == truth and r["decision"] == decision
            for r in rows
        )

    metrics = {
        g: {
            "self_accepted": count(g, "self", "self"),
            "self_unknown": count(g, "self", "unknown"),
            "negative_self": count(g, "non-self", "self"),
        }
        for g in ("paired", "production_ownership")
    }
    expected_baseline = {
        "paired": {"self_accepted": 6, "self_unknown": 6, "negative_self": 0},
        "production_ownership": {
            "self_accepted": 2,
            "self_unknown": 10,
            "negative_self": 0,
        },
    }
    single_false_self_count = sum(
        r["truth"] == "non-self"
        and r["current_single_default_batch"]["decision"] == "self"
        for r in raw
    )
    if verify_prior_baseline:
        assert metrics == expected_baseline
        assert single_false_self_count == 3
    assert integrity(c) == read(output / "before/fingerprints.json")
    write(
        target,
        {
            "previous_manifest_sha256": digest(previous_manifest),
            "previous_replay_sha256": digest(previous_replay),
            "production_batch_size": 8,
            "production_writes": False,
            "verify_prior_baseline": verify_prior_baseline,
            "metrics": metrics,
            "events": rows,
            "single_default_batch": raw,
            "batch_context_comparison": contexts,
            "scope_limit": "counterfactual automatic projection in memory; live human identity untouched; not official Blind scores",
        },
    )
    c.close()
    clone.close()
    return metrics
