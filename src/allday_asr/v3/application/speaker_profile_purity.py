"""Clip-level audit evidence; never mutates person facts or matcher profiles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from allday_asr.v3.adapters.sqlite.people_sample_eligibility import usable_voice_sample

AUDIT_VERSION = "speaker-profile-purity-v1"
PURITIES = {"clean_single", "mixed_overlap", "boundary_cross", "uncertain"}
QUALITY_FLAGS = {"too_short", "noisy", "distant", "distorted", "low_volume", "other"}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_key(media_id: str, start_ms: int, end_ms: int) -> str:
    return digest([media_id, start_ms, end_ms])


def matching_prototypes(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    """Mirror person_vectors' current eligibility, including policy and grant."""
    rows = connection.execute(
        f"""SELECT p.*, person.display_name, t.session_id
        FROM voice_prototypes p
        JOIN persons person ON person.person_id=p.person_id
        JOIN speaker_tracks t ON t.speaker_track_id=p.speaker_track_id
        WHERE p.status='accepted' AND p.human_confirmed=1
          AND {usable_voice_sample('p')}
          AND person.kind='known'
          AND p.quality_score>=COALESCE((SELECT minimum_quality
            FROM person_identity_policy_revisions policy
            WHERE policy.person_id=p.person_id ORDER BY policy.revision DESC LIMIT 1),0.5)
          AND EXISTS(SELECT 1 FROM person_cluster_links link
            WHERE link.cluster_id=p.cluster_id AND link.person_id=p.person_id
              AND link.status='active')
          AND COALESCE((SELECT review.decision FROM voice_prototype_reviews review
            WHERE review.prototype_id=COALESCE(p.source_prototype_id,p.prototype_id)
              AND review.person_id=p.person_id
            ORDER BY review.created_at DESC,review.review_id DESC LIMIT 1),'confirmed')='confirmed'
        ORDER BY p.person_id,p.prototype_id"""
    ).fetchall()
    return [dict(row) for row in rows]


def fact_provenance(
    connection: sqlite3.Connection, media_id: str, start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT fact.fact_id,fact.dimension,fact.value_json,fact.actor,
          fact.state,fact.source_utterance_id,fact.payload_json,
          audio.start_ms,audio.end_ms
        FROM annotation_fact_audio audio
        JOIN annotation_facts fact ON fact.fact_id=audio.fact_id
        WHERE audio.media_id=? AND audio.start_ms<? AND audio.end_ms>?
        ORDER BY fact.created_at,fact.fact_id""",
        (media_id, end_ms, start_ms),
    ).fetchall()
    return [
        {**dict(row), "value": json.loads(row["value_json"]),
         "payload": json.loads(row["payload_json"])}
        for row in rows
    ]


def profile_provenance(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for prototype in matching_prototypes(connection):
        sample_sets = [dict(row) for row in connection.execute(
            """SELECT sample_key,facts_json,windows_json,current FROM annotation_sample_sets
            WHERE prototype_id IN (?,?)""",
            (prototype["prototype_id"], prototype["source_prototype_id"]),
        )]
        for clip in json.loads(prototype["representative_clips_json"]):
            media_id = clip["media_id"]
            start_ms, end_ms = int(clip["start_ms"]), int(clip["end_ms"])
            facts = fact_provenance(connection, media_id, start_ms, end_ms)
            result.append({
                "person_id": prototype["person_id"],
                "person": prototype["display_name"],
                "prototype_id": prototype["prototype_id"],
                "source_prototype_id": prototype["source_prototype_id"],
                "prototype_status": prototype["status"],
                "sample_ids": [row["sample_key"] for row in sample_sets],
                "sample_current": [bool(row["current"]) for row in sample_sets],
                "source_session_id": prototype["session_id"],
                "source_media_id": media_id,
                "start_ms": start_ms, "end_ms": end_ms,
                "source_track_id": prototype["speaker_track_id"],
                "source_cluster_id": prototype["cluster_id"],
                "person_facts": [row["fact_id"] for row in facts if row["dimension"] == "person"],
                "person_fact_sources": [row["actor"] for row in facts if row["dimension"] == "person"],
                "human_batch_provenance": [
                    {"fact_id": row["fact_id"], "actor": row["actor"],
                     "payload": row["payload"]}
                    for row in facts if row["dimension"] == "person"
                ],
                "source_key": source_key(media_id, start_ms, end_ms),
                "model": prototype["model"],
                "model_version": prototype["model_version"],
                "embedding_hash": digest(json.loads(prototype["vector_json"])),
            })
    return result


def latest_reviews(
    connection: sqlite3.Connection, task_id: str | None = None
) -> dict[str, dict[str, Any]]:
    where = "WHERE review.task_id=? AND" if task_id is not None else "WHERE"
    return {
        row["task_id"]: dict(row)
        for row in connection.execute(
            f"""SELECT review.* FROM speaker_profile_purity_reviews review
            {where} NOT EXISTS(SELECT 1 FROM speaker_profile_purity_reviews newer
              WHERE newer.task_id=review.task_id AND newer.revision>review.revision)""",
            (task_id,) if task_id is not None else (),
        )
    }


def list_phone_tasks(
    connection: sqlite3.Connection, *, history: bool = False,
    task_id: str | None = None,
) -> list[dict[str, Any]]:
    latest = latest_reviews(connection, task_id)
    result = []
    where = "WHERE task.task_id=?" if task_id is not None else ""
    rows = connection.execute(
        f"""SELECT task.*, run.audit_version, person.display_name
        FROM speaker_profile_purity_tasks task
        JOIN speaker_profile_purity_runs run ON run.audit_run_id=task.audit_run_id
        JOIN persons person ON person.person_id=task.target_person_id
        {where}
        ORDER BY CASE task.priority WHEN 'P0-special' THEN 0 WHEN 'P0' THEN 1
          WHEN 'P1' THEN 2 ELSE 3 END,task.created_at,task.task_id""",
        (task_id,) if task_id is not None else (),
    )
    for row in rows:
        task = dict(row)
        review = latest.get(task["task_id"])
        reviewed = review is not None and review["action"] == "submit"
        if reviewed != history:
            continue
        review_id = f"purity:{task['task_id']}"
        clip = {"media_id": task["source_media_id"], "start_ms": task["start_ms"],
                "end_ms": task["end_ms"]}
        candidate = {
            "prototype_id": task["task_id"], "session_id": task["source_session_id"] or "",
            "speaker_track_id": task["source_track_id"] or "",
            "person_name": "待听原音", "representative_clips": [clip],
            "review_status": "reviewed" if reviewed else "pending",
        }
        context: dict[str, Any] = {
            "voice_mode": "speaker_profile_purity", "review_lane": "history" if reviewed else "primary",
            "voice_candidates": [candidate], "prototype_ids": [task["task_id"]],
            "audit_version": task["audit_version"],
            "duration_ms": task["end_ms"] - task["start_ms"],
        }
        if reviewed:
            context["purity_review"] = {
                "primary_speaker_person_id": review["primary_speaker_person_id"],
                "primary_speaker_unknown": bool(review["primary_speaker_unknown"]),
                "purity": review["purity"],
                "other_speaker_ids": json.loads(review["other_speaker_ids_json"]),
                "quality_flags": json.loads(review["quality_flags_json"]),
                "revision": review["revision"],
            }
            context["target_person_id"] = task["target_person_id"]
            context["needs_correction_review"] = (
                review["primary_speaker_person_id"] is not None
                and review["primary_speaker_person_id"] != task["target_person_id"]
            )
        result.append({
            "review_id": review_id, "kind": "speaker_profile_purity",
            "priority": "high" if task["priority"].startswith("P0") else "normal",
            "source_id": task["task_id"], "source_revision": review["revision"] if review else None,
            "session_id": task["source_session_id"], "person_id": None,
            "title": "声纹纯度审核", "summary": "听原音后判断是谁，以及目标片段是否纯净",
            "reason": "speaker_profile_purity", "evidence_count": 1,
            "created_at": task["created_at"],
            "updated_at": review["reviewed_at"] if review else task["created_at"],
            "context": context,
        })
    return result


def append_review(
    connection: sqlite3.Connection, task_id: str, payload: dict[str, Any], device_id: str
) -> dict[str, Any]:
    operation_id = payload.get("operation_id")
    if operation_id is not None:
        if not isinstance(operation_id, str) or not operation_id.isalnum() or len(operation_id) > 64:
            raise ValueError("purity operation id is invalid")
        existing = connection.execute(
            "SELECT * FROM speaker_profile_purity_reviews WHERE review_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            if existing["task_id"] != task_id or existing["action"] != payload.get("action"):
                raise ValueError("purity operation id was reused")
            if existing["action"] == "submit" and (
                existing["primary_speaker_person_id"] != payload.get("primary_speaker_person_id")
                or bool(existing["primary_speaker_unknown"]) != (payload.get("primary_speaker_unknown") is True)
                or existing["purity"] != payload.get("purity")
                or json.loads(existing["other_speaker_ids_json"]) != payload.get("other_speaker_ids", [])
                or json.loads(existing["quality_flags_json"]) != payload.get("quality_flags", [])
            ):
                raise ValueError("purity operation id was reused with a different answer")
            return {"review_id": operation_id, "task_id": task_id,
                    "revision": existing["revision"], "action": existing["action"],
                    "reviewed_at": existing["reviewed_at"]}
    task = connection.execute(
        "SELECT task_id FROM speaker_profile_purity_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if task is None:
        raise ValueError("purity audit task does not exist")
    latest = latest_reviews(connection, task_id).get(task_id)
    action = payload.get("action")
    if action == "undo":
        if latest is None or latest["action"] != "submit":
            raise ValueError("only a submitted purity review can be undone")
        person_id, unknown, purity, others, flags = None, 1, None, [], []
    elif action == "submit":
        person_id = payload.get("primary_speaker_person_id")
        unknown = payload.get("primary_speaker_unknown") is True
        purity = payload.get("purity")
        others = payload.get("other_speaker_ids", [])
        flags = payload.get("quality_flags", [])
        if (not unknown and not isinstance(person_id, str)) or (unknown and person_id is not None):
            raise ValueError("choose one primary speaker or unknown")
        if person_id is not None and connection.execute(
            "SELECT 1 FROM persons WHERE person_id=?", (person_id,)
        ).fetchone() is None:
            raise ValueError("primary speaker does not exist")
        if purity not in PURITIES:
            raise ValueError("purity verdict is invalid")
        if not isinstance(others, list) or any(not isinstance(v, str) for v in others):
            raise ValueError("other speakers are invalid")
        if any(v != "unknown" and connection.execute("SELECT 1 FROM persons WHERE person_id=?", (v,)).fetchone() is None for v in others):
            raise ValueError("other speaker does not exist")
        if not isinstance(flags, list) or any(v not in QUALITY_FLAGS for v in flags):
            raise ValueError("quality flags are invalid")
        if purity != "mixed_overlap" and others:
            raise ValueError("other speakers only apply to mixed audio")
    else:
        raise ValueError("purity review action is invalid")
    revision = (latest["revision"] if latest else 0) + 1
    reviewed_at = datetime.now(timezone.utc).isoformat()
    review_id = operation_id or uuid4().hex
    connection.execute(
        """INSERT INTO speaker_profile_purity_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (review_id, task_id, revision, person_id, int(unknown), purity,
         json.dumps(others), json.dumps(flags), f"phone:{device_id}", action, reviewed_at),
    )
    return {"review_id": review_id, "task_id": task_id, "revision": revision,
            "action": action, "reviewed_at": reviewed_at}
