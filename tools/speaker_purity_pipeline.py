"""Private, reproducible purity shadow rollout. Never enables production use.

python tools/speaker_purity_pipeline.py all
python tools/speaker_purity_pipeline.py migrate|build|probe|evaluate
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from allday_asr.v3.adapters.files import ContentAddressedStore  # noqa: E402
from allday_asr.v3.adapters.models.funasr import FunASRBackend, _cached_model_or_id  # noqa: E402
from allday_asr.v3.adapters.speaker_embeddings.funasr import (  # noqa: E402
    FunASRSpeakerEmbeddingProvider,
)
from allday_asr.v3.adapters.sqlite.migration_runner import V3MigrationRunner  # noqa: E402
from allday_asr.v3.adapters.sqlite.speaker_purity_repository import (  # noqa: E402
    current_evidence,
    reconcile,
    propose_recrop,
    register_candidates,
)
from allday_asr.v3.adapters.purity_shadow import build_shadow, unit  # noqa: E402
from allday_asr.v3.adapters.query_purity import (  # noqa: E402
    fit_clean_thresholds,
    grouped_validation,
    query_features,
)
from allday_asr.v3.adapters.sqlite.speaker_profile_purity import digest, source_key  # noqa: E402
from allday_asr.v3.domain.people import RepresentativeClip, SpeakerEmbedding  # noqa: E402
from allday_asr.v3.domain.speaker_purity import source_ineligibility  # noqa: E402
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput  # noqa: E402

AUDIT = ROOT / "outputs/speaker-profile-purity-audit-20260928"
V2 = ROOT / "outputs/speaker-recognition-benchmark-v2-20260928"
OLD = ROOT / "outputs/annotation-feasibility-20260928"
BLIND_SETTINGS = (
    ROOT / "outputs/speaker-identity-blind-validation-20260928/frozen-candidates.json"
)
DEFAULT_OUT = ROOT / "outputs/enrollment-purity-gate-20260930"
PROTECTED = (
    "annotation_facts",
    "annotation_fact_audio",
    "voice_prototypes",
    "voice_prototype_reviews",
    "persons",
    "person_cluster_links",
    "person_identity_policy_revisions",
    "speaker_profile_purity_reviews",
    "speaker_profile_purity_tasks",
    "speaker_match_decisions",
    "annotation_sample_sets",
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def write(out, name, value):
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def fingerprint(connection):
    # Sorted by full row content, independent of SQLite row order.
    return {
        table: digest(
            sorted(
                [list(r) for r in connection.execute(f"SELECT * FROM {table}")],
                key=lambda r: json.dumps(r, sort_keys=True),
            )
        )
        for table in PROTECTED
    }


class RawVectors:
    """Content-addressed cache made exclusively by re-extracting original audio.

    Does not read prototype vectors or previous experiment embedding caches.
    Includes model/config/audio hashes and crop preprocessing in its cache key.
    """

    def __init__(self, connection, out, model_hash, audio_root):
        self.connection, self.out, self.model_hash = connection, out, model_hash
        self.store = ContentAddressedStore(audio_root)
        self.values = (
            dict(np.load(out / "raw-vectors.npz"))
            if (out / "raw-vectors.npz").exists()
            else {}
        )
        self.media = {}
        backend = FunASRBackend()
        self.provider = FunASRSpeakerEmbeddingProvider(
            self.store, backend_factory=lambda: backend, temp_root=out / "tmp"
        )
        self.model, self.model_version = (
            self.provider.model,
            self.provider.model_version,
        )

    def resolve(self, media_id):
        if media_id not in self.media:
            rows = self.connection.execute(
                """SELECT a.sha256,r.storage_key,a.duration_ms
                FROM audio_assets a JOIN audio_replicas r USING(asset_id)
                WHERE a.media_id=? AND r.state='available' ORDER BY r.verified_at DESC""",
                (media_id,),
            ).fetchall()
            self.media[media_id] = None
            for row in rows:
                path = self.store.path_for(row["storage_key"])
                if path.is_file() and sha256(path) == row["sha256"]:
                    self.media[media_id] = dict(row)
                    break
        return self.media[media_id]

    def key(self, window):
        media = self.resolve(window["media_id"])
        if (
            media is None
            or window["start_ms"] < 0
            or window["end_ms"] > media["duration_ms"]
        ):
            raise FileNotFoundError("verified original audio range unavailable")
        return digest(
            [
                media["sha256"],
                window["start_ms"],
                window["end_ms"],
                self.model_hash,
                self.model,
                self.model_version,
                "extract_clip-16k-mono-v1",
            ]
        )

    def prepare(self, windows):
        missing = {}
        for window in windows:
            key = self.key(window)
            if key not in self.values:
                missing[key] = window
        if not missing:
            return
        tracks = tuple(
            SpeakerTrackInput(
                key,
                "",
                (
                    SpeakerClipInput(
                        w["media_id"],
                        self.resolve(w["media_id"])["storage_key"],
                        w["start_ms"],
                        w["end_ms"],
                        None,
                    ),
                ),
            )
            for key, w in missing.items()
        )
        print(f"Re-extracting {len(tracks)} raw CAM++ windows", flush=True)
        for embedding in self.provider.embed(tracks):
            self.values[embedding.speaker_track_id] = np.asarray(embedding.vector)
        if set(missing) - set(self.values):
            raise RuntimeError("raw extraction incomplete")
        temporary = self.out / "raw-vectors.tmp.npz"
        np.savez_compressed(temporary, **self.values)
        temporary.replace(self.out / "raw-vectors.npz")

    def vector(self, window):
        return unit(self.values[self.key(window)])

    def embed(self, tracks):
        windows = [
            {
                "media_id": c.media_id,
                "start_ms": c.source_start_ms,
                "end_ms": c.source_end_ms,
            }
            for t in tracks
            for c in t.clips
        ]
        self.prepare(windows)
        return tuple(
            SpeakerEmbedding(
                t.speaker_track_id,
                self.model,
                self.model_version,
                tuple(
                    unit(
                        np.mean(
                            [
                                self.vector(
                                    {
                                        "media_id": c.media_id,
                                        "start_ms": c.source_start_ms,
                                        "end_ms": c.source_end_ms,
                                    }
                                )
                                for c in t.clips
                            ],
                            axis=0,
                        )
                    )
                ),
                tuple(
                    RepresentativeClip(c.media_id, c.source_start_ms, c.source_end_ms)
                    for c in t.clips
                ),
                min(
                    1, sum(c.source_end_ms - c.source_start_ms for c in t.clips) / 12000
                ),
            )
            for t in tracks
        )


def sources(connection, raw):
    tasks = [
        dict(r)
        for r in connection.execute(
            "SELECT * FROM speaker_profile_purity_tasks ORDER BY task_id"
        )
    ]
    result = []
    for task in tasks:
        from allday_asr.v3.domain.dataset_roles import is_learning
        if not is_learning(connection, task['source_session_id']):
            continue
        key = source_key(task["source_media_id"], task["start_ms"], task["end_ms"])
        media = raw.resolve(task["source_media_id"])
        result.append(
            task
            | {
                "source_key": key,
                'dataset_role': 'learning',
                "evidence": current_evidence(connection, key),
                "audio_available": media is not None
                and task["end_ms"] <= media["duration_ms"],
                "storage_key": media["storage_key"] if media else None,
                "active_profile_source": "active_profile_source"
                in json.loads(task["reason_codes_json"]),
            }
        )
    return result


def coverage(connection, rows, centers):
    result = []
    dates, fallback_sessions = {}, set()
    for r in connection.execute(
        "SELECT session_id,captured_start,timezone FROM recording_sessions"
    ):
        try:
            tz = ZoneInfo(r["timezone"])
        except (ZoneInfoNotFoundError, TypeError):
            tz = ZoneInfo(
                "Asia/Singapore"
            )  # Explicit client timezone, never host timezone.
            fallback_sessions.add(r["session_id"])
        captured = (
            datetime.fromisoformat(r["captured_start"]) if r["captured_start"] else None
        )
        if captured is not None and captured.tzinfo is None:
            captured = captured.replace(tzinfo=tz)
        dates[r["session_id"]] = (
            captured.astimezone(tz).date().isoformat() if captured else ""
        )
    for person in connection.execute(
        "SELECT person_id,display_name,kind FROM persons WHERE kind!='unknown'"
    ):
        active = [
            r
            for r in rows
            if r["target_person_id"] == person["person_id"]
            and r["active_profile_source"]
        ]
        clean = [
            r
            for r in active
            if source_ineligibility(
                r["evidence"], person["person_id"], audio_available=r["audio_available"]
            )
            is None
        ]
        excluded = Counter(
            source_ineligibility(
                r["evidence"], person["person_id"], audio_available=r["audio_available"]
            )
            for r in active
            if r not in clean
        )
        sessions = sorted(
            {r["source_session_id"] for r in clean if r["source_session_id"]}
        )
        clean_dates = sorted({dates[s] for s in sessions if dates.get(s)})
        old_sessions = {
            r["source_session_id"] for r in active if r["source_session_id"]
        }
        old_dates = {dates[s] for s in old_sessions if dates.get(s)}
        person_centers = [c for c in centers if c["person_id"] == person["person_id"]]
        ready = bool(person_centers) and len(sessions) >= 2
        result.append(
            {
                "person_id": person["person_id"],
                "person": person["display_name"],
                "kind": person["kind"],
                "legacy_sources": len(active),
                "clean_sources": len(clean),
                "excluded": dict(excluded),
                "excluded_contamination": sum(excluded.values()),
                "clean_duration_s": sum(r["end_ms"] - r["start_ms"] for r in clean)
                / 1000,
                "clean_sessions": len(sessions),
                "clean_dates": len(clean_dates),
                "session_ids": sessions,
                "dates": clean_dates,
                "date_timezone_basis": "session IANA timezone; invalid legacy labels use explicit client Asia/Singapore",
                "date_timezone_fallback_sessions": sorted(
                    set(sessions) & fallback_sessions
                ),
                "sources_per_session": dict(
                    Counter(r["source_session_id"] for r in clean)
                ),
                "shadow_centers": len(person_centers),
                "profile_ready": ready,
                "selected_clean_sources": sum(
                    len(c["source_keys"]) for c in person_centers
                ),
                "selected_clean_duration_s": sum(
                    c["duration_ms"] for c in person_centers
                )
                / 1000,
                "status": "profile_ready" if ready else "purity_profile_insufficient",
                "reason": "minimum quality and session diversity recommendation met"
                if ready
                else "no qualifying session centroid"
                if not person_centers
                else "single-session seed; diversity unproven",
                "legacy_profile_ready": bool(active),
                "would_lose_profile": bool(active) and not person_centers,
                "would_reduce_session_coverage": len(sessions) < len(old_sessions),
                "would_reduce_date_coverage": len(clean_dates) < len(old_dates),
                "needs_more_clean_enrollment": not ready,
            }
        )
    return result


def build(connection, raw, out):
    rows = sources(connection, raw)
    eligible = [
        r
        for r in rows
        if r["active_profile_source"]
        and source_ineligibility(
            r["evidence"], r["target_person_id"], audio_available=r["audio_available"]
        )
        is None
    ]
    raw.prepare(
        [
            {
                "media_id": r["source_media_id"],
                "start_ms": r["start_ms"],
                "end_ms": r["end_ms"],
            }
            for r in eligible
        ]
    )
    # Respect each person's existing minimum quality, including non-default revisions.
    centers = []
    for person_id in sorted({r["target_person_id"] for r in eligible}):
        row = connection.execute(
            """SELECT minimum_quality FROM person_identity_policy_revisions
            WHERE person_id=? ORDER BY revision DESC LIMIT 1""",
            (person_id,),
        ).fetchone()
        centers.extend(
            build_shadow(
                [r for r in eligible if r["target_person_id"] == person_id],
                raw,
                minimum_quality=row[0] if row else 0.5,
            )
        )
    covered = coverage(connection, rows, centers)
    write(
        out,
        "clean-profile-coverage.json",
        {
            "people": covered,
            "minimum_viable_recommendation": {
                "window_ms": 800,
                "session_windows_max": 5,
                "centroid_duration_ms": "12000 * existing person minimum_quality (default 6000)",
                "session_diversity": "at least two independent sessions recommended; one-session seed insufficient for canary",
                "basis": "worker min window, provider duration quality and existing person policy; diversity is a recommendation, not a tuned gate",
            },
        },
    )
    write(
        out,
        "profile-migration-plan.json",
        {"people": covered, "production_mode": "legacy", "switch": False},
    )
    write(
        out,
        "shadow-profile.json",
        {
            "profile_mode": "purity_shadow",
            "model": raw.model,
            "model_version": raw.model_version,
            "model_fingerprint": raw.model_hash,
            "representation": "longest <=5 reviewed windows per session; unit centroid; max session cosine",
            "centers": [c | {"vector": c["vector"].tolist()} for c in centers],
            "eligible_pool": [
                {
                    k: r[k]
                    for k in (
                        "source_key",
                        "source_media_id",
                        "start_ms",
                        "end_ms",
                        "target_person_id",
                        "source_session_id",
                    )
                }
                | {"evidence_id": r["evidence"].evidence_id}
                for r in eligible
            ],
        },
    )
    correction, boundary = [], []
    for row in rows:
        evidence = row["evidence"]
        detail = {
            k: row[k]
            for k in (
                "task_id",
                "source_key",
                "target_person_id",
                "source_media_id",
                "start_ms",
                "end_ms",
                "source_session_id",
                "source_track_id",
                "source_cluster_id",
            )
        }
        if (
            evidence.primary_person_id
            and evidence.primary_person_id != row["target_person_id"]
        ):
            chain = []
            for prototype_id in json.loads(row["source_snapshot_json"]).get(
                "prototype_ids", []
            ):
                prototype = connection.execute(
                    """SELECT prototype_id,source_prototype_id,speaker_track_id,
                    cluster_id,person_id,status,quality_score,human_confirmed,created_at
                    FROM voice_prototypes WHERE prototype_id=?""",
                    (prototype_id,),
                ).fetchone()
                if prototype is None:
                    continue
                ids = (prototype_id, prototype["source_prototype_id"])
                chain.append(
                    dict(prototype)
                    | {
                        "reviews": [
                            dict(r)
                            for r in connection.execute(
                                "SELECT * FROM voice_prototype_reviews WHERE prototype_id IN (?,?)",
                                ids,
                            )
                        ],
                        "sample_sets": [
                            dict(r)
                            for r in connection.execute(
                                """SELECT sample_key,session_id,
                        person_id,prototype_id,facts_json,windows_json,current
                        FROM annotation_sample_sets WHERE prototype_id IN (?,?)""",
                                ids,
                            )
                        ],
                    }
                )
            correction.append(
                detail
                | {
                    "review_primary_person_id": evidence.primary_person_id,
                    "candidate_only": True,
                    "provenance": json.loads(row["source_snapshot_json"]),
                    "prototype_chain": chain,
                }
            )
        if evidence.verdict == "boundary_cross":
            boundary.append(
                detail
                | {"status": "requires_new_range_and_review", "auto_recrop": False}
            )
    write(out, "historical-label-correction-candidates.json", correction)
    write(out, "recrop-candidates.json", boundary)
    root_cause_report(out, correction)
    print(
        f"Built {len(centers)} session centroids from {len(eligible)} eligible sources",
        flush=True,
    )


def subwindows(row):
    # Nonoverlapping 2s probes; tail retained down to provider's 0.8s worker minimum.
    # Subdivision is feature extraction, never a recrop purity grant.
    media = row.get("media_id", row.get("source_media_id"))
    result = []
    start = row["start_ms"]
    while start < row["end_ms"]:
        end = min(start + 2000, row["end_ms"])
        if row["end_ms"] - end < 800:
            end = row["end_ms"]
        if end - start >= 800:
            result.append(
                {
                    "media_id": media,
                    "start_ms": start,
                    "end_ms": end,
                    "window_id": source_key(media, start, end),
                    "speaker_track_id": row.get(
                        "source_track_id", row.get("speaker_track_id")
                    ),
                    "cluster_id": row.get("source_cluster_id", row.get("cluster_id")),
                }
            )
        start = end
    return result


def components(rows):
    # Shared media joins session groups, preventing correlated crop leakage.
    parent = {}

    def root(x):
        parent.setdefault(x, x)
        if parent[x] != x:
            parent[x] = root(parent[x])
        return parent[x]

    for r in rows:
        a, b = (
            root("s:" + (r["source_session_id"] or r["source_media_id"])),
            root("m:" + r["source_media_id"]),
        )
        parent[max(a, b)] = min(a, b)
    return {r["source_key"]: root("m:" + r["source_media_id"]) for r in rows}


def status_metrics(rows):
    result = {}
    for gold in (
        "clean_single",
        "mixed_overlap",
        "boundary_cross",
        "wrong_primary",
        "uncertain",
        "unreviewed",
    ):
        selected = [r for r in rows if r["gold"] == gold]
        counts = Counter(r["status"] for r in selected)
        n = len(selected)
        result[gold] = {
            "total": n,
            "status_counts": dict(counts),
            "pass_rate": counts["PASS"] / n if n else None,
            "detection_rate": counts["SUSPICIOUS"] / n if n else None,
            "insufficient_rate": counts["INSUFFICIENT"] / n if n else None,
            "assessable": n - counts["INSUFFICIENT"],
            "conditional_pass_rate": counts["PASS"] / (n - counts["INSUFFICIENT"])
            if n > counts["INSUFFICIENT"]
            else None,
            "conditional_detection_rate": counts["SUSPICIOUS"]
            / (n - counts["INSUFFICIENT"])
            if n > counts["INSUFFICIENT"]
            else None,
        }
    result["clean_retention"] = result["clean_single"]["pass_rate"]
    result["false_suspicious_rate"] = result["clean_single"]["detection_rate"]
    return result


def root_cause_report(out, correction):
    lines = [
        "# Sample worker root-cause analysis (private)",
        "",
        "Person fact and speaker-profile eligibility were previously coupled. Cluster-level attribution can be correct "
        "while particular enrollment windows contain another speaker. This is an architectural responsibility, not a claim that bulk labeling is unreasonable.",
        "",
        "1. annotation_sample_plan.compute_plans verifies active, non-conflicting person/sound facts and available audio. "
        "It merges overlapping/touching person ranges. It does not check acoustic overlap or cross-speaker consistency.",
        "2. Selection splits ranges into <=8s chunks, considers <=40s per contiguous range, keeps >=0.8s windows, "
        "and picks the longest five per person/session. Duration preference can retain mixtures and erase utterance boundaries.",
        "3. FunASRSpeakerEmbeddingProvider normalizes each CAM++ vector and averages it. Its quality score is only "
        "min(1,total_range_duration/12s); no purity score is present. Enough aggregate duration can hide a short wrong-primary source.",
        "4. Prototype confirmation checks model, cluster-person link and aggregate quality. It does not require individual source purity. "
        "Accepted copies preserve the aggregate embedding and all representative sources.",
        "5. person_vectors checks accepted/human-confirmed, model, active grant, current sample eligibility, policy and latest prototype review. "
        "A prototype-level confirmation cannot establish that each of its original windows is clean.",
        "",
        "## Exact wrong-primary chain",
        "",
    ]
    for item in correction:
        if "active_profile_source" not in item["provenance"].get("reason_codes", []):
            continue
        p = item["provenance"]
        lines += [
            f"Target {item['target_person_id']}; reviewed primary {item['review_primary_person_id']}. "
            f"Source {item['source_media_id']} [{item['start_ms']},{item['end_ms']})ms.",
            f"Fact(s) {p.get('person_facts')}; cluster {item['source_cluster_id']}; track {item['source_track_id']}.",
        ]
        for proto in item["prototype_chain"]:
            lines += [
                f"Accepted prototype {proto['prototype_id']}; source prototype {proto['source_prototype_id']}; "
                f"quality {proto['quality_score']}; human_confirmed {proto['human_confirmed']}; reviews {proto['reviews']}; "
                f"sample sets {proto['sample_sets']}."
            ]
        lines += [
            "The same wrong-primary source survived two successive sampled aggregates and confirmations. "
            "The first aggregate already passed duration quality despite that window; the later aggregate also retained it. "
            "Missing constraints were source-level purity before embedding, and source-level purity authorization at profile admission. "
            "The review primary is not inferred from the original person fact. No fact is corrected here.",
            "",
        ]
    lines += [
        "## Future candidate review",
        "",
        "The worker records only its bounded selected enrollment candidates in a separate source-deduplicated queue. "
        "Unreviewed means candidate-only in purity shadow, never clean. Capacity limits review demand; coverage gaps and new sessions rank first. "
        "The CLI exports candidates and supports contained recrop proposals, each requiring a fresh review. "
        "No tasks are generated for every utterance and no new phone UI is required.",
    ]
    (out / "root-cause-analysis.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def run_probe(connection, raw, out):
    rows = sources(connection, raw)
    group = components(rows)
    reviewed = []
    raw.prepare([w for r in rows if r["audio_available"] for w in subwindows(r)])
    for row in rows:
        evidence = row["evidence"]
        gold = str(evidence.verdict)
        if (
            evidence.primary_person_id
            and evidence.primary_person_id != row["target_person_id"]
        ):
            gold = "wrong_primary"
        windows = subwindows(row) if row["audio_available"] else []
        for w in windows:
            w["embedding"] = raw.vector(w).tolist()
        reviewed.append(
            {
                "source_key": row["source_key"],
                "task_id": row["task_id"],
                "gold": gold,
                "intrinsic_purity": str(evidence.verdict),
                "group": group[row["source_key"]],
                "features": query_features(windows),
                "windows": windows,
                "mapping": "exact reviewed clip subdivided for consistency features",
            }
        )
    cv = grouped_validation(reviewed)
    cv["metrics"] = {
        rule: status_metrics(values) for rule, values in cv["outcomes"].items()
    }
    thresholds = fit_clean_thresholds(reviewed)
    cv["full_fit_shadow_thresholds"] = thresholds
    cv["capability_limit"] = (
        "Wrong-primary may be a single pure speaker; this probe cannot verify identity. Uncertain is never enrollment eligible."
    )
    cv["benchmark_unit"] = (
        "one exact reviewed source range; not 169 independently verified multi-window identity queries"
    )
    write(out, "query-purity-features.json", reviewed)
    write(out, "query-purity-evaluation.json", cv)
    print(
        f"Probe validation: {cv['groups']} independent session/media groups", flush=True
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("all", "migrate", "build", "probe", "evaluate", "queue", "recrop"),
    )
    parser.add_argument("--db", type=Path, default=ROOT / "state/v3/core.sqlite3")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--audio-root", type=Path, default=ROOT / "state/v3/audio")
    parser.add_argument("--parent-source")
    parser.add_argument("--target-person")
    parser.add_argument("--media-id")
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--end-ms", type=int)
    args = parser.parse_args()
    out = args.out.resolve()
    if not out.is_relative_to(ROOT / "outputs"):
        parser.error("private output must stay in ignored repository outputs/")
    out.mkdir(parents=True, exist_ok=True)
    if not args.db.is_file():
        parser.error("existing reviewed database required")
    with sqlite3.connect(args.db) as before:
        before.row_factory = sqlite3.Row
        baseline = fingerprint(before)
        if not (out / "pre-migration.sqlite3").exists():
            with sqlite3.connect(out / "pre-migration.sqlite3") as backup:
                before.backup(backup)
    V3MigrationRunner(args.db).initialize()
    connection = sqlite3.connect(args.db, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    now = datetime.now(timezone.utc).isoformat()
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        summary = reconcile(connection, now)
    write(out, "migration-summary.json", summary)
    if args.command == "recrop":
        if None in (
            args.parent_source,
            args.target_person,
            args.media_id,
            args.start_ms,
            args.end_ms,
        ):
            parser.error("recrop requires parent, target, media and new start/end")
        with connection:
            propose_recrop(
                connection,
                args.parent_source,
                args.target_person,
                args.media_id,
                args.start_ms,
                args.end_ms,
                now,
                {"proposal_source": "offline-cli"},
            )
    if args.command == "queue":
        from allday_asr.v3.adapters.sqlite.annotation_sample_plan import plans

        sessions = [
            r[0]
            for r in connection.execute(
                "SELECT DISTINCT session_id FROM annotation_sample_sets WHERE current=1"
            )
        ]
        with connection:
            for session in sessions:
                for plan in plans(
                    connection, None, session, "FunASR/CAM++", "v1-local"
                )[0]:
                    register_candidates(connection, plan, now)
        write(
            out,
            "candidate-queue.json",
            [dict(r) for r in connection.execute("SELECT * FROM purity_candidates")],
        )
    if args.command in ("all", "build", "probe", "evaluate"):
        frozen = read(
            BLIND_SETTINGS
        )  # Configuration only; no future blind audio or results read.
        model_dir = Path(_cached_model_or_id("cam++"))
        model_files = {
            p.name: sha256(p) for p in sorted(model_dir.iterdir()) if p.is_file()
        }
        if model_files.get("campplus_cn_common.bin") != frozen["model_sha256"]:
            raise RuntimeError(
                "CAM++ weights differ from frozen production/audit model"
            )
        raw = RawVectors(connection, out, digest(model_files), args.audio_root)
        if (
            sha256(V2 / "manifest.json") != frozen["source_sha256"]["manifest.json"]
            or sha256(OLD / "vectors.npz")
            != frozen["source_sha256"]["pilot-vectors.npz"]
        ):
            raise RuntimeError("frozen historical split/vector cache changed")
        write(
            out,
            "model-lock.json",
            {
                "model": raw.model,
                "version": raw.model_version,
                "model_files": model_files,
                "fingerprint": raw.model_hash,
                "frozen_matcher": frozen,
                "input_hashes": {
                    str(p.relative_to(ROOT)): sha256(p)
                    for p in (
                        BLIND_SETTINGS,
                        V2 / "manifest.json",
                        V2 / "track-level-results.json",
                        AUDIT / "verification.json",
                    )
                },
            },
        )
        if args.command in ("all", "build"):
            build(connection, raw, out)
        if args.command in ("all", "probe"):
            run_probe(connection, raw, out)
        if args.command in ("all", "evaluate"):
            from speaker_purity_evaluation import evaluate

            evaluate(connection, raw, out, frozen)
        if model_files != {
            p.name: sha256(p) for p in sorted(model_dir.iterdir()) if p.is_file()
        }:
            raise RuntimeError("CAM++ model files changed during extraction")
    after = fingerprint(connection)
    with sqlite3.connect(out / "pre-migration.sqlite3") as backup:
        migration_baseline = fingerprint(backup)
    verification = {
        "completed_command": args.command,
        "tests": read(out / "test-verification.json")
        if (out / "test-verification.json").exists()
        else None,
        "schema_version": V3MigrationRunner(args.db).schema_version(),
        "protected_tables_unchanged": baseline == after,
        "before": baseline,
        "after": after,
        "pre_migration_schema_version": V3MigrationRunner(
            out / "pre-migration.sqlite3"
        ).schema_version(),
        "pre_migration_protected_tables_unchanged": migration_baseline == after,
        "foreign_key_errors": [
            list(r) for r in connection.execute("PRAGMA foreign_key_check")
        ],
        "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "production_profile_mode": "legacy",
        "production_identity_changed": False,
        "production_profile_changed": False,
        "G_P_changed": False,
        "CAMpp_changed": False,
        "phone_repository_changed": False,
        "artifact_sha256": {
            p.name: sha256(p)
            for p in sorted(out.iterdir())
            if p.is_file()
            and p.suffix in (".json", ".md", ".npz")
            and p.name != "verification.json"
        },
    }
    write(out, "verification.json", verification)
    connection.close()
    if (
        baseline != after
        or verification["foreign_key_errors"]
        or verification["integrity_check"] != "ok"
    ):
        raise RuntimeError("verification failed; do not recommend canary")
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "reviewed",
                    "clean",
                    "mixed",
                    "boundary",
                    "wrong",
                    "uncertain",
                    "conflicting",
                    "unresolved",
                )
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
