"""Summarize human clip reviews and compare current/clean CAM++ profiles offline."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allday_asr.v3.adapters.files import ContentAddressedStore  # noqa: E402
from allday_asr.v3.adapters.speaker_embeddings.funasr import (  # noqa: E402
    FunASRSpeakerEmbeddingProvider,
)
from allday_asr.v3.adapters.sqlite.speaker_profile_purity import (  # noqa: E402
    digest, latest_reviews,
)
from allday_asr.v3.ports.speaker_embeddings import (  # noqa: E402
    SpeakerClipInput, SpeakerTrackInput,
)
OUT = ROOT / "outputs/speaker-profile-purity-audit-20260928"
V2 = ROOT / "outputs/speaker-recognition-benchmark-v2-20260928"
OLD = ROOT / "outputs/annotation-feasibility-20260928"
BLIND = ROOT / "outputs/speaker-identity-blind-validation-20260928"
DB = ROOT / "state/v3/core.sqlite3"


def unit(value: np.ndarray) -> np.ndarray:
    return value / np.linalg.norm(value)


def frozen_queries(manifest: dict, windows: dict, vectors: dict, split: str) -> list[dict]:
    """Mirror V2's five-window controlled group definition without retuning."""
    result = []
    for group in manifest["groups"]:
        if group["split"] != split:
            continue
        ids = group["windows"]
        for index in range(0, len(ids), 5):
            chunk = ids[index:index + 5]
            seconds = sum((windows[key]["end_ms"] - windows[key]["start_ms"]) / 1000 for key in chunk)
            array = np.stack([vectors[key] for key in chunk])
            center = unit(np.mean(array, axis=0))
            consistency = float(np.mean(array @ center))
            result.append({"id": f"{group['session_id']}:{group['person_id']}:{index}:groups:None",
                           "truth": group["person_id"], "session": group["session_id"],
                           "date": group["date"], "windows": chunk, "duration_s": seconds,
                           "quality": float(min(1, seconds / 6) * max(0, consistency)),
                           "vector": center})
    return result


def score_rows(queries: list[dict], refs: dict, frozen: dict, *, per_person: bool) -> list[dict]:
    people = sorted(refs)
    rows = []
    for query in queries:
        ordered = sorted(
            ((person, max(float(query["vector"] @ reference) for reference in refs[person]))
             for person in people), key=lambda value: (-value[1], value[0])
        )
        best, score = ordered[0]
        second, second_score = ordered[1]
        if per_person:
            threshold = frozen["P"]["per_person"].get(best, frozen["P"]["global_fallback"])
            margin_gate = frozen["P"]["margin"]
        else:
            threshold = frozen["G"]["threshold"]
            margin_gate = frozen["G"]["margin"]
        accepted = (query["duration_s"] >= frozen["minimum_speech_seconds"]
                    and score >= threshold and score - second_score >= margin_gate)
        rows.append({key: value for key, value in query.items() if key != "vector"} |
                    {"best": best, "score": score, "second": second,
                     "second_score": second_score, "margin": score - second_score,
                     "prediction": best if accepted else None})
    return rows


def metrics(rows: list[dict], known: set[str]) -> dict:
    counts: Counter = Counter()
    for row in rows:
        truth, predicted = row["truth"], row["prediction"]
        if truth in known:
            counts["known"] += 1
            counts["correct" if predicted == truth else "false_reject" if predicted is None else "wrong_person"] += 1
        else:
            counts["unknown"] += 1
            counts["unknown_false_accept" if predicted else "unknown_reject"] += 1
    counts["known_recall"] = counts["correct"] / counts["known"] if counts["known"] else None
    counts["unknown_false_accept_rate"] = counts["unknown_false_accept"] / counts["unknown"] if counts["unknown"] else None
    return dict(counts)


def save(name: str, value: object) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def read(name: str, root: Path = OUT):
    return json.loads((root / name).read_text(encoding="utf-8"))


def clip_vectors(connection: sqlite3.Connection, rows: list[dict]) -> dict[str, np.ndarray]:
    path = OUT / "clip-embeddings.npz"
    values = dict(np.load(path)) if path.exists() else {}
    clean_path = OUT / "clean-clip-embeddings.npz"
    if clean_path.exists():
        values.update(dict(np.load(clean_path)))
    missing = [row for row in rows if row["task_id"] not in values]
    if not missing:
        return values
    tracks = []
    for row in missing:
        replica = connection.execute(
            """SELECT replica.storage_key FROM audio_assets asset
            JOIN audio_replicas replica ON replica.asset_id=asset.asset_id
            WHERE asset.media_id=? AND replica.state='available'
            ORDER BY replica.verified_at DESC LIMIT 1""",
            (row["source_media_id"],),
        ).fetchone()
        if replica is None:
            continue
        tracks.append(SpeakerTrackInput(
            row["task_id"], row["source_session_id"] or "",
            (SpeakerClipInput(row["source_media_id"], replica["storage_key"],
                              row["start_ms"], row["end_ms"], None),),
        ))
    provider = FunASRSpeakerEmbeddingProvider(
        ContentAddressedStore(ROOT / "state/v3/audio"), temp_root=OUT / "tmp"
    )
    for result in provider.embed(tuple(tracks)):
        values[result.speaker_track_id] = np.asarray(result.vector, dtype=np.float32)
    np.savez_compressed(OUT / "clean-clip-embeddings.npz", **values)
    return values


def summarize(tasks: list[dict], reviews: dict[str, dict],
              focus_id: str, self_id: str) -> dict:
    people: dict[str, dict] = {}
    clusters: dict[str, dict] = {}
    by_person: dict[str, list[dict]] = defaultdict(list)
    for task in tasks:
        if "active_profile_source" in json.loads(task["reason_codes_json"]):
            by_person[task["target_person_id"]].append(task)
    for person_id, sources in by_person.items():
        counts: Counter = Counter()
        for task in sources:
            review = reviews.get(task["task_id"])
            if review is None or review["action"] != "submit":
                continue
            counts["reviewed"] += 1
            purity = review["purity"]
            counts[purity] += 1
            if review["primary_speaker_person_id"] != person_id:
                counts["wrong_person" if not review["primary_speaker_unknown"] else "unknown_person"] += 1
            if review["primary_speaker_person_id"] == person_id and purity == "clean_single":
                counts["clean_target"] += 1
        reviewed = counts["reviewed"]
        contaminated = sum(1 for task in sources if (review := reviews.get(task["task_id"]))
                           and review["action"] == "submit" and
                           (review["purity"] in {"mixed_overlap", "boundary_cross"}
                            or review["primary_speaker_person_id"] not in {person_id, None}))
        people[person_id] = {"active_profile_source_count": len(sources), **counts,
                             "unreviewed": len(sources) - reviewed,
                             "clean_rate_among_reviewed": counts["clean_target"] / reviewed if reviewed else None,
                             "contamination_rate_among_reviewed": contaminated / reviewed if reviewed else None}
    for task in tasks:
        cluster_id = task["source_cluster_id"] or "unclustered"
        review = reviews.get(task["task_id"])
        if review is None or review["action"] != "submit":
            continue
        key = f"{task['target_person_id']}:{cluster_id}"
        item = clusters.setdefault(key, {"person_id": task["target_person_id"],
                                         "cluster_id": cluster_id, "reviewed_clips": 0,
                                         "clean_target": 0, "wrong_speaker": 0,
                                         "mixed": 0, "uncertain": 0})
        item["reviewed_clips"] += 1
        if review["primary_speaker_person_id"] == task["target_person_id"] and review["purity"] == "clean_single":
            item["clean_target"] += 1
        if review["primary_speaker_person_id"] not in {task["target_person_id"], None}:
            item["wrong_speaker"] += 1
        if review["purity"] in {"mixed_overlap", "boundary_cross"}:
            item["mixed"] += 1
        if review["purity"] == "uncertain" or review["primary_speaker_unknown"]:
            item["uncertain"] += 1
    focus_sources = by_person.get(focus_id, [])
    self_found = any(
        (review := reviews.get(task["task_id"])) is not None and review["action"] == "submit"
        and (review["primary_speaker_person_id"] == self_id or
             self_id in json.loads(review["other_speaker_ids_json"]))
        for task in focus_sources
    )
    complete = bool(focus_sources) and all(
        (review := reviews.get(task["task_id"])) is not None and review["action"] == "submit"
        for task in focus_sources
    )
    answer = "YES" if self_found else "NO" if complete else "INSUFFICIENT REVIEW"
    return {"status": "REVIEWED" if people and all(v["unreviewed"] == 0 for v in people.values()) else "WAITING_FOR_HUMAN_REVIEW",
            "people": people, "focus_self_in_enrollment": answer,
            "clusters": list(clusters.values()),
            "cluster_note": "These are reviewed clip counts, not verified cluster population rates."}


def refs_current(connection: sqlite3.Connection, graph: list[dict], known: set[str]) -> dict[str, list[np.ndarray]]:
    refs: dict[str, list[np.ndarray]] = defaultdict(list)
    seen: set[str] = set()
    for source in graph:
        prototype_id = source["prototype_id"]
        if source["person_id"] not in known or prototype_id in seen:
            continue
        seen.add(prototype_id)
        row = connection.execute(
            "SELECT vector_json FROM voice_prototypes WHERE prototype_id=?", (prototype_id,)
        ).fetchone()
        if row is None or digest(json.loads(row[0])) != source["embedding_hash"]:
            raise RuntimeError("frozen profile vector missing or changed")
        refs[source["person_id"]].append(unit(np.asarray(json.loads(row[0]))))
    return dict(refs)


def refs_clean(tasks: list[dict], reviews: dict[str, dict], vectors: dict[str, np.ndarray],
               known: set[str]) -> dict[str, list[np.ndarray]]:
    by_prototype: dict[tuple[str, str], list[np.ndarray]] = defaultdict(list)
    for task in tasks:
        if task["target_person_id"] not in known or task["task_id"] not in vectors:
            continue
        review = reviews.get(task["task_id"])
        if (review is None or review["action"] != "submit"
                or review["primary_speaker_person_id"] != task["target_person_id"]
                or review["purity"] != "clean_single"):
            continue
        snapshot = json.loads(task["source_snapshot_json"])
        if "active_profile_source" not in snapshot["reason_codes"]:
            continue
        for prototype_id in snapshot["prototype_ids"]:
            by_prototype[(task["target_person_id"], prototype_id)].append(vectors[task["task_id"]])
    refs: dict[str, list[np.ndarray]] = defaultdict(list)
    for (person_id, _prototype), items in by_prototype.items():
        refs[person_id].append(unit(np.mean(items, axis=0)))
    return dict(refs)


def compare_queries(queries: list[dict], current: dict, clean: dict, frozen: dict) -> dict:
    if len(current) < 2 or len(clean) < 2:
        return {"status": "INSUFFICIENT_CLEAN_PROFILE"}
    result = {}
    for label, refs in (("current", current), ("human_verified_clean", clean)):
        g = score_rows(queries, refs, frozen, per_person=False)
        p = score_rows(queries, refs, frozen, per_person=True)
        result[label] = {"G": g, "P": p,
                         "G_metrics": metrics(g, set(frozen["known_ids"])),
                         "P_metrics": metrics(p, set(frozen["known_ids"]))}
    return result


def clean_failure_query_sensitivity(neighbor: dict, tasks: list[dict], reviews: dict,
                                    vectors: dict, self_id: str, focus_id: str,
                                    current: dict, clean: dict, frozen: dict) -> dict:
    """Inspect query purity and rescore only individually clean self windows.

    This deliberately changes the frozen query composition, so its scores are
    exploratory and must not replace the frozen benchmark result.
    """
    task_by_id = {task["task_id"]: task for task in tasks}
    audit = []
    queries = []
    for event in neighbor.get("failure_events", []):
        keys = event["query_sources"]
        clean_keys = [key for key in keys if (review := reviews.get(key))
                      and review["action"] == "submit"
                      and review["primary_speaker_person_id"] == self_id
                      and review["purity"] == "clean_single"]
        mixed_focus = sum(
            (review := reviews.get(key)) is not None
            and review["purity"] == "mixed_overlap"
            and (review["primary_speaker_person_id"] == focus_id
                 or focus_id in json.loads(review["other_speaker_ids_json"]))
            for key in keys
        )
        audit.append({"failure_id": event["failure_id"], "query_sources": len(keys),
                      "clean_self_sources": len(clean_keys),
                      "mixed_sources": sum(reviews.get(key, {}).get("purity") == "mixed_overlap"
                                           for key in keys),
                      "mixed_with_focus_sources": mixed_focus,
                      "all_clean_self": len(clean_keys) == len(keys)})
        if not clean_keys or any(key not in vectors or key not in task_by_id for key in clean_keys):
            continue
        queries.append({"id": event["failure_id"], "truth": self_id,
                        "session": "", "date": "", "windows": clean_keys,
                        "duration_s": sum((task_by_id[key]["end_ms"] - task_by_id[key]["start_ms"]) / 1000
                                          for key in clean_keys),
                        "quality": 1.0,
                        "vector": unit(np.mean([vectors[key] for key in clean_keys], axis=0))})
    complete = len(queries) == len(audit)
    return {"status": "EXPLORATORY" if complete else "INCOMPLETE_QUERY_EMBEDDINGS",
            "query_review": audit,
            "comparison": compare_queries(queries, current, clean, frozen) if complete else None,
            "note": "Only individually reviewed clean-self query windows are averaged. "
                    "This changes frozen V2 query composition and is a sensitivity check, "
                    "not the frozen benchmark."}


def main() -> None:
    verification = read("verification.json")
    focus_id = verification["focus_person_id"]
    self_id = verification["self_person_id"]
    conn = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    tasks = [dict(row) for row in conn.execute("SELECT * FROM speaker_profile_purity_tasks")]
    reviews = latest_reviews(conn)
    summary = summarize(tasks, reviews, focus_id, self_id)
    save("purity-summary.json", summary)
    priority_counts = dict(Counter(r["priority"] for r in tasks))
    save("review-progress.json", {"total": len(tasks), "reviewed": sum(r["action"] == "submit" for r in reviews.values()),
                                   "pending": len(tasks) - sum(r["action"] == "submit" for r in reviews.values()),
                                   "counts": priority_counts, "by_priority": priority_counts,
                                   "audit_run_ids": sorted({r["audit_run_id"] for r in tasks})})
    clean_rows = [task for task in tasks if (review := reviews.get(task["task_id"]))
                  and review["action"] == "submit" and review["primary_speaker_person_id"] == task["target_person_id"]
                  and review["purity"] == "clean_single"
                  and "active_profile_source" in json.loads(task["reason_codes_json"])]
    save("clean-profile-results.json", {"status": summary["status"],
                                        "human_verified_clean_pool": [row["task_id"] for row in clean_rows]})
    if summary["status"] != "REVIEWED":
        save("failure-comparison.json", {"status": "WAITING_FOR_HUMAN_REVIEW",
                                         "focus_self_in_enrollment": summary["focus_self_in_enrollment"]})
        print(json.dumps({"status": summary["status"], "reviewed": sum(r["action"] == "submit" for r in reviews.values()),
                          "active_source_people": summary["people"]}, ensure_ascii=False))
        return
    run_ids = {row["audit_run_id"] for row in tasks}
    if len(run_ids) != 1:
        save("failure-comparison.json", {"status": "MULTIPLE_PROFILE_SNAPSHOTS",
                                         "audit_run_ids": sorted(run_ids)})
        return
    frozen_graph = json.loads(conn.execute(
        "SELECT source_snapshot_json FROM speaker_profile_purity_runs WHERE audit_run_id=?",
        (next(iter(run_ids)),),
    ).fetchone()[0])
    frozen = read("frozen-candidates.json", BLIND)
    known = set(frozen["known_ids"])
    current = refs_current(conn, frozen_graph, known)
    vectors = clip_vectors(conn, clean_rows)
    clean = refs_clean(tasks, reviews, vectors, known)
    if set(clean) != known:
        save("failure-comparison.json", {"status": "INSUFFICIENT_CLEAN_PROFILE", "people_with_clean_sources": list(clean)})
        return
    manifest = read("manifest.json", V2)
    ws = {row["id"]: row for row in manifest["windows"]}
    cached = dict(np.load(OLD / "vectors.npz"))
    controlled = frozen_queries(manifest, ws, cached, "test")
    controlled_result = compare_queries(controlled, current, clean, frozen)
    track_rows = read("track-level-results.json", V2)["tracks"]
    track_queries = []
    for track in track_rows:
        if track["status"] != "fully_labelled_single_person":
            continue
        row = conn.execute("SELECT vector_json FROM voice_prototypes WHERE prototype_id=?",
                           (track["prototype_id"],)).fetchone()
        if row is None:
            continue
        track_queries.append({"id": track["prototype_id"], "truth": track["truth_id"],
                              "session": track["session_id"], "date": track["date"],
                              "windows": track["clips"], "duration_s": track["duration_s"],
                              "quality": 1.0, "vector": unit(np.asarray(json.loads(row[0])))})
    track_result = compare_queries(track_queries, current, clean, frozen)
    failure_cases = [x for x in read("failure-cases.json", V2)
                     if x.get("unit") == "real_track_representative" and x.get("truth") == self_id
                     and x.get("prediction") == focus_id and x.get("method") == "historical calibrated centroid"]
    failure_ids = {x["id"] for x in failure_cases}
    failure_result = {label: {rule: [row for row in result[rule] if row["id"] in failure_ids]
                              for rule in ("G", "P")}
                      for label, result in track_result.items()
                      if label in {"current", "human_verified_clean"}}
    current_failures = failure_result.get("current", {}).get("G", [])
    clean_failures = failure_result.get("human_verified_clean", {}).get("G", [])
    current_wrong = any(row["prediction"] == focus_id for row in current_failures)
    clean_wrong = any(row["prediction"] == focus_id for row in clean_failures)
    current_score = max((row["score"] for row in current_failures), default=None)
    clean_score = max((row["score"] for row in clean_failures), default=None)
    neighbor = read("failure-nearest-neighbors.json")
    sensitivity = clean_failure_query_sensitivity(neighbor, tasks, reviews, vectors,
                                                  self_id, focus_id, current, clean, frozen)
    save("clean-query-sensitivity.json", sensitivity)
    wrong_ids = {row["id"] for row in current_failures if row["prediction"] == focus_id}
    query_confound = any(not row["all_clean_self"] for row in sensitivity["query_review"]
                         if row["failure_id"] in wrong_ids)
    if summary["focus_self_in_enrollment"] == "YES" and current_wrong and not clean_wrong:
        conclusion = ("A (query-confounded): profile contamination exists and clean profile "
                      "removes the frozen G errors, but mixed failure queries prevent "
                      "attributing those errors solely to profile contamination"
                      if query_confound else "A: strong evidence for H1")
    elif summary["focus_self_in_enrollment"] == "YES" and clean_wrong:
        conclusion = "B: H1 exists, H2 remains possible"
    elif summary["focus_self_in_enrollment"] == "NO" and clean_wrong:
        conclusion = "C: evidence favors H2"
    else:
        conclusion = "INCONCLUSIVE: inspect score change and source overlap"
    nearest = {}
    for event in neighbor.get("failure_events", []):
        nearest[event["failure_id"]] = [{**r, "purity_review":
            reviews.get(r["source_key"], {}).get("purity"),
            "review_primary": reviews.get(r["source_key"], {}).get("primary_speaker_person_id")}
            for r in event["neighbors"][:5]]
    save("clean-profile-results.json", {"status": "COMPUTED", "known_ids": list(known),
                                        "clean_pool_count": len(clean_rows),
                                        "clean_profile_centers": {p: len(v) for p, v in clean.items()},
                                        "current_profile_centers": {p: len(v) for p, v in current.items()},
                                        "controlled": controlled_result, "track": track_result,
                                        "retrospective_split_note": "V2 test labels and G/P gates frozen; current accepted profile may contain later audio. Inspect source overlap before causal claims."})
    save("failure-comparison.json", {"status": "COMPUTED", "failure_ids": list(failure_ids),
                                     "comparison": failure_result, "nearest_source_reviews": nearest,
                                     "focus_self_in_enrollment": summary["focus_self_in_enrollment"],
                                     "mixed_failure_query_confound": query_confound,
                                     "query_review": sensitivity["query_review"],
                                     "current_best_score": current_score,
                                     "clean_best_score": clean_score,
                                     "conclusion": conclusion})
    print(json.dumps({"status": "COMPUTED", "clean_pool": len(clean_rows),
                      "focus_self_in_enrollment": summary["focus_self_in_enrollment"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
