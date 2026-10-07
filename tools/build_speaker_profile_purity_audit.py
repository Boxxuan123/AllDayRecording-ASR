"""Create a reproducible, deduplicated clip-level speaker purity audit.

Writes only isolated audit tables and ignored private outputs. Never changes
person facts, prototypes, policy, or matching behavior.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allday_asr.v3.adapters.files import ContentAddressedStore  # noqa: E402
from allday_asr.v3.adapters.speaker_embeddings.funasr import (  # noqa: E402
    FunASRSpeakerEmbeddingProvider,
)
from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner  # noqa: E402
from allday_asr.v3.adapters.sqlite.speaker_profile_purity import (  # noqa: E402
    AUDIT_VERSION, digest, fact_provenance, profile_provenance, source_key,
)
from allday_asr.v3.ports.speaker_embeddings import (  # noqa: E402
    SpeakerClipInput, SpeakerTrackInput,
)

DEFAULT_DB = ROOT / "state/v3/core.sqlite3"
OUT = ROOT / "outputs/speaker-profile-purity-audit-20260928"
V2 = ROOT / "outputs/speaker-recognition-benchmark-v2-20260928"
OLD = ROOT / "outputs/annotation-feasibility-20260928"
PRIORITY = {"P0-special": 0, "P0": 1, "P1": 2, "P2": 3}


def write(name: str, value: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def connection(db: Path, *, readonly: bool) -> sqlite3.Connection:
    if not readonly:
        V3MigrationRunner(db).initialize()
    uri = f"file:{db.as_posix()}?mode={'ro' if readonly else 'rw'}"
    value = sqlite3.connect(uri, uri=True, timeout=30)
    value.row_factory = sqlite3.Row
    if readonly:
        value.execute("PRAGMA query_only=ON")
    return value


def add(pool: dict[str, dict], row: dict, priority: str, reason: str) -> None:
    key = source_key(row["source_media_id"], row["start_ms"], row["end_ms"])
    entry = pool.get(key)
    if entry is None:
        entry = {**row, "source_key": key, "priority": priority,
                 "reason_codes": [], "prototype_ids": [], "sample_ids": []}
        pool[key] = entry
    if PRIORITY[priority] < PRIORITY[entry["priority"]]:
        entry["priority"] = priority
    if reason not in entry["reason_codes"]:
        entry["reason_codes"].append(reason)
    for label in ("prototype_id", "sample_ids"):
        values = row.get(label)
        if label == "prototype_id":
            values = [values] if values else []
            target = entry["prototype_ids"]
        else:
            target = entry["sample_ids"]
        for value in values or []:
            if value not in target:
                target.append(value)


def media_storage(conn: sqlite3.Connection, media_id: str) -> str | None:
    row = conn.execute(
        """SELECT replica.storage_key FROM audio_assets asset
        JOIN audio_replicas replica ON replica.asset_id=asset.asset_id
        WHERE asset.media_id=? AND replica.state='available'
        ORDER BY replica.verified_at DESC LIMIT 1""", (media_id,)
    ).fetchone()
    return str(row[0]) if row else None


def focus_people(conn: sqlite3.Connection, requested: str | None) -> tuple[str, str, str]:
    self_row = conn.execute(
        "SELECT person_id FROM persons WHERE kind='self' LIMIT 1"
    ).fetchone()
    if self_row is None:
        raise RuntimeError("No self person exists in the current database")
    if requested:
        focus = conn.execute(
            "SELECT person_id, display_name FROM persons WHERE person_id=? OR display_name=?",
            (requested, requested),
        ).fetchone()
    else:
        # Use the largest active, human-attributed person fact set as the
        # default focus. The explicit option keeps other audits reproducible.
        focus = conn.execute(
            """SELECT p.person_id, p.display_name, COUNT(*) AS fact_count
            FROM persons p JOIN annotation_facts f
              ON json_extract(f.value_json, '$')=p.person_id
            WHERE p.kind='known' AND f.dimension='person' AND f.state='active'
              AND (f.actor='legacy-human' OR f.actor LIKE 'phone-operation:%'
                   OR f.actor LIKE 'human%')
            GROUP BY p.person_id ORDER BY fact_count DESC, p.person_id LIMIT 1"""
        ).fetchone()
    if focus is None:
        raise RuntimeError("No focus person found; pass --focus-person")
    return str(focus["person_id"]), str(focus["display_name"]), str(self_row["person_id"])


def failure_cases(focus_id: str, self_id: str) -> list[dict]:
    path = V2 / "failure-cases.json"
    if not path.exists():
        return []
    all_cases = json.loads(path.read_text(encoding="utf-8"))
    unique: dict[str, dict] = {}
    for item in all_cases:
        if (item.get("unit") == "real_track_representative"
                and item.get("truth") == self_id
                and item.get("prediction") == focus_id):
            unique[item["id"]] = item
    return list(unique.values())


def cached_candidates(conn: sqlite3.Connection, pool: dict[str, dict], limit: int,
                      focus_id: str, focus_name: str, self_id: str) -> list[dict]:
    manifest_path, vector_path = V2 / "manifest.json", OLD / "vectors.npz"
    if not manifest_path.exists() or not vector_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vectors = np.load(vector_path)
    windows = [w for w in manifest["windows"] if w["id"] in vectors]
    self_vectors = [vectors[w["id"]] for w in windows if w["person_id"] == self_id]
    focus_vectors = [vectors[w["id"]] for w in windows if w["person_id"] == focus_id]
    if not self_vectors or not focus_vectors:
        return []
    self_center = np.mean(self_vectors, axis=0)
    self_center /= np.linalg.norm(self_center)
    focus_center = np.mean(focus_vectors, axis=0)
    focus_center /= np.linalg.norm(focus_center)
    ranked = []
    for w in windows:
        if w["person_id"] != focus_id:
            continue
        key = source_key(w["media_id"], w["start_ms"], w["end_ms"])
        if key in pool:
            continue
        facts = fact_provenance(conn, w["media_id"], w["start_ms"], w["end_ms"])
        persons = [f for f in facts if f["dimension"] == "person" and f["state"] == "active"]
        if not persons or any(f["value"] != focus_id for f in persons):
            continue
        vector = vectors[w["id"]]
        track = persons[0]["payload"].get("speaker_track_id")
        cluster = conn.execute(
            """SELECT cluster_id FROM speaker_cluster_memberships
            WHERE speaker_track_id=? AND state='active' ORDER BY rowid LIMIT 1""", (track,)
        ).fetchone()
        ranked.append({
            "source_key": key, "person_id": focus_id, "person": focus_name,
            "source_session_id": w["session_id"], "source_media_id": w["media_id"],
            "start_ms": w["start_ms"], "end_ms": w["end_ms"],
            "source_track_id": track, "source_cluster_id": cluster[0] if cluster else None,
            "person_facts": [f["fact_id"] for f in persons],
            "person_fact_sources": [f["actor"] for f in persons],
            "human_batch_provenance": [{"fact_id": f["fact_id"], "actor": f["actor"],
                                        "payload": f["payload"]} for f in persons],
            "self_similarity": float(vector @ self_center),
            "focus_similarity": float(vector @ focus_center),
            "duration_ms": w["end_ms"] - w["start_ms"],
            "embedding_cache_id": w["id"],
        })
    if not ranked:
        return []
    by_key = {r["source_key"]: r for r in ranked}
    for row in sorted(ranked, key=lambda r: -r["self_similarity"])[:max(3, limit // 3)]:
        add(pool, row, "P1", "self_similarity_high")
    for row in sorted(ranked, key=lambda r: r["focus_similarity"])[:max(3, limit // 3)]:
        add(pool, row, "P1", "person_outlier")
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in ranked:
        groups[row["source_cluster_id"] or "unclustered"].append(row)
    rng = random.Random(20260928)
    for group in groups.values():
        choices = [
            (max(group, key=lambda r: r["focus_similarity"]), "cluster_spotcheck_centroid"),
            (min(group, key=lambda r: r["focus_similarity"]), "cluster_spotcheck_outlier"),
            (max(group, key=lambda r: r["self_similarity"]), "cluster_spotcheck_self_like"),
            (max(group, key=lambda r: r["duration_ms"]), "cluster_spotcheck_longest"),
            (rng.choice(group), "cluster_spotcheck_random"),
        ]
        for row, reason in choices:
            add(pool, row, "P1", reason)
    # Control sources are stratified by person, date, session and cluster; only
    # individually selected windows receive tasks, never entire clusters.
    control_groups: dict[tuple, list[dict]] = defaultdict(list)
    for w in windows:
        if w["person_id"] not in set(manifest["names"]):
            continue
        key = source_key(w["media_id"], w["start_ms"], w["end_ms"])
        if key in pool:
            continue
        control_groups[(w["person_id"], w.get("date"), w["session_id"])].append(w)
    for group_key in sorted(control_groups, key=str):
        if sum(v["priority"] == "P2" for v in pool.values()) >= 6:
            break
        w = rng.choice(control_groups[group_key])
        facts = fact_provenance(conn, w["media_id"], w["start_ms"], w["end_ms"])
        add(pool, {
            "person_id": w["person_id"], "person": manifest["names"].get(w["person_id"], ""),
            "source_session_id": w["session_id"], "source_media_id": w["media_id"],
            "start_ms": w["start_ms"], "end_ms": w["end_ms"],
            "source_track_id": None, "source_cluster_id": None,
            "person_facts": [f["fact_id"] for f in facts if f["dimension"] == "person"],
            "person_fact_sources": [f["actor"] for f in facts if f["dimension"] == "person"],
            "human_batch_provenance": [], "embedding_cache_id": w["id"],
        }, "P2", "random_control")
    return sorted(by_key.values(), key=lambda r: (-r["self_similarity"], r["source_key"]))


def nearest_neighbors(conn: sqlite3.Connection, pool: dict[str, dict], failures: list[dict],
                      focus_id: str, self_name: str, *, compute: bool) -> dict:
    profile = [r for r in pool.values() if r["person_id"] == focus_id
               and "active_profile_source" in r["reason_codes"]]
    queries: dict[str, dict] = {}
    for case in failures:
        for w in case["windows"]:
            key = source_key(w["media_id"], w["start_ms"], w["end_ms"])
            queries[key] = {"source_key": key, "person_id": case["truth"], "person": self_name,
                            "source_session_id": case["session"],
                            "source_media_id": w["media_id"], "start_ms": w["start_ms"],
                            "end_ms": w["end_ms"], "source_track_id": None,
                            "source_cluster_id": None, "person_facts": [],
                            "person_fact_sources": [], "human_batch_provenance": []}
            add(pool, queries[key], "P0-special", "failure_query")
    if not compute or not profile or not queries:
        return {"status": "EMBEDDINGS_NOT_COMPUTED", "failure_query_sources": list(queries),
                "profile_source_count": len(profile)}
    items = list({r["source_key"]: r for r in [*profile, *queries.values()]}.values())
    tracks = []
    for row in items:
        storage = media_storage(conn, row["source_media_id"])
        if not storage:
            continue
        tracks.append(SpeakerTrackInput(row["source_key"], row["source_session_id"] or "",
                                  (SpeakerClipInput(row["source_media_id"], storage,
                                                    row["start_ms"], row["end_ms"], None),)))
    provider = FunASRSpeakerEmbeddingProvider(ContentAddressedStore(ROOT / "state/v3/audio"),
                                              temp_root=OUT / "tmp")
    embeddings = provider.embed(tuple(tracks))
    vectors = {e.speaker_track_id: np.asarray(e.vector, dtype=np.float32) for e in embeddings}
    np.savez_compressed(OUT / "clip-embeddings.npz", **vectors)
    comparisons = []
    for case in failures:
        keys = [source_key(w["media_id"], w["start_ms"], w["end_ms"]) for w in case["windows"]]
        present = [vectors[k] for k in keys if k in vectors]
        if not present:
            continue
        query = np.mean(present, axis=0)
        query /= np.linalg.norm(query)
        neighbors = sorted(
            ({"source_key": r["source_key"], "cosine": float(query @ vectors[r["source_key"]]),
              "media_id": r["source_media_id"], "start_ms": r["start_ms"], "end_ms": r["end_ms"]}
             for r in profile if r["source_key"] in vectors),
            key=lambda r: -r["cosine"],
        )
        for neighbor in neighbors[:5]:
            add(pool, next(r for r in profile if r["source_key"] == neighbor["source_key"]),
                "P0-special", "failure_nearest_neighbor")
        comparisons.append({"failure_id": case["id"], "query_sources": keys,
                            "neighbors": neighbors})
    return {"status": "COMPUTED", "model": provider.model,
            "model_version": provider.model_version, "failure_events": comparisons,
            "embedding_cache_hash": digest({k: v.tolist() for k, v in vectors.items()})}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--person", help="person ID or display name")
    parser.add_argument("--focus-person", help="person ID or display name for failure analysis")
    parser.add_argument("--priority", choices=tuple(PRIORITY))
    parser.add_argument("--limit", type=int, default=20, help="maximum P1 sources")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-embeddings", action="store_true")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connection(args.db, readonly=args.dry_run)
    focus_id, focus_name, self_id = focus_people(conn, args.focus_person)
    self_name = conn.execute("SELECT display_name FROM persons WHERE person_id=?", (self_id,)).fetchone()[0]
    graph = profile_provenance(conn)
    pool: dict[str, dict] = {}
    for row in graph:
        add(pool, row, "P0", "active_profile_source")
    failures = failure_cases(focus_id, self_id)
    ranking = cached_candidates(conn, pool, args.limit, focus_id, focus_name, self_id)
    neighbor = nearest_neighbors(conn, pool, failures, focus_id, self_name,
                                 compute=not args.skip_embeddings)
    values = sorted(pool.values(), key=lambda r: (PRIORITY[r["priority"]], r["source_key"]))
    # P0 and failure evidence are mandatory; --limit only caps P1.
    p1 = [r for r in values if r["priority"] == "P1"][:args.limit]
    values = [r for r in values if r["priority"] != "P1"] + p1
    if args.person:
        values = [r for r in values if args.person in (r["person_id"], r["person"])]
    if args.priority:
        values = [r for r in values if r["priority"] == args.priority]
    missing = [r for r in values if media_storage(conn, r["source_media_id"]) is None]
    values = [r for r in values if r not in missing]
    snapshot_hash = digest(graph)
    embedding_hash = neighbor.get("embedding_cache_hash", "not-computed")
    audit_run_id = digest([AUDIT_VERSION, snapshot_hash, embedding_hash])
    now = datetime.now(timezone.utc).isoformat()
    created = 0
    stale_existing = 0
    if not args.dry_run:
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO speaker_profile_purity_runs VALUES(?,?,?,?,?,?,?,?)""",
                (audit_run_id, AUDIT_VERSION, "FunASR/CAM++", "v1-local",
                 embedding_hash, snapshot_hash, json.dumps(graph, ensure_ascii=False), now),
            )
            for row in values:
                task_id = row["source_key"]
                old = conn.execute(
                    "SELECT source_snapshot_json FROM speaker_profile_purity_tasks WHERE task_id=?",
                    (task_id,),
                ).fetchone()
                if old is not None and json.loads(old[0]).get("person_id") != row["person_id"]:
                    stale_existing += 1
                inserted = conn.execute(
                    """INSERT OR IGNORE INTO speaker_profile_purity_tasks
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, audit_run_id, row["source_key"], row["person_id"],
                     row["source_media_id"], row["start_ms"], row["end_ms"],
                     row.get("source_session_id"), row.get("source_track_id"),
                     row.get("source_cluster_id"), row["priority"],
                     json.dumps(row["reason_codes"]), json.dumps(row, ensure_ascii=False), now),
                )
                created += inserted.rowcount
    reviewed = 0
    if not args.dry_run:
        reviewed = conn.execute(
            """SELECT COUNT(*) FROM speaker_profile_purity_reviews review
            WHERE review.action='submit' AND NOT EXISTS(
              SELECT 1 FROM speaker_profile_purity_reviews newer
              WHERE newer.task_id=review.task_id AND newer.revision>review.revision)"""
        ).fetchone()[0]
    counts = {p: sum(r["priority"] == p for r in values) for p in PRIORITY}
    write("provenance.json", graph)
    write("review-candidates.json", values)
    write("risk-ranking.json", ranking)
    write("failure-nearest-neighbors.json", neighbor)
    write("review-progress.json", {"audit_run_id": audit_run_id, "total": len(values),
                                   "created": created, "counts": counts,
                                   "missing_audio": missing, "reviewed": reviewed,
                                   "stale_existing_target": stale_existing})
    write("verification.json", {"audit_version": AUDIT_VERSION,
                                "focus_person_id": focus_id, "self_person_id": self_id,
                                "profile_snapshot_hash": snapshot_hash,
                                "embedding_hash": embedding_hash,
                                "campp_model_sha256": json.loads((V2 / "verification.json").read_text(encoding="utf-8")).get("model_sha256") if (V2 / "verification.json").exists() else None,
                                "dry_run": args.dry_run, "created_at": now})
    for name in ("clean-profile-results", "failure-comparison", "purity-summary"):
        path = OUT / f"{name}.json"
        if not path.exists():
            write(f"{name}.json", {"status": "WAITING_FOR_HUMAN_REVIEW"})
    print(json.dumps({"total": len(values), "created": created, "counts": counts,
                      "missing_audio": len(missing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
