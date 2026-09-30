"""Reconcile latest effective reviews without changing facts or review history.

Caller owns the transaction. Contradictory tasks block the source; revisions
only supersede earlier answers within the SAME task, never across tasks.
"""

import json
from collections import Counter, defaultdict

from allday_asr.v3.application.speaker_profile_purity import digest, source_key
from allday_asr.v3.domain.speaker_purity import (
    PurityVerdict,
    SourcePurityEvidence,
    source_ineligibility,
)


def ensure_source(connection, media_id, start_ms, end_ms):
    key = source_key(media_id, start_ms, end_ms)
    connection.execute(
        "INSERT OR IGNORE INTO speaker_purity_sources VALUES(?,?,?,?)",
        (key, media_id, start_ms, end_ms),
    )
    return key


def current_evidence(connection, key):
    row = connection.execute(
        """SELECT e.*,s.source_media_id,s.start_ms,s.end_ms FROM speaker_purity_current c
        JOIN speaker_source_purity_evidence e ON e.evidence_id=c.evidence_id
        JOIN speaker_purity_sources s ON s.source_key=c.source_key
        WHERE c.source_key=?""",
        (key,),
    ).fetchone()
    if row is None:
        return None
    references = json.loads(row["review_references_json"])
    task_ids = {
        r[0]
        for r in connection.execute(
            """SELECT task_id FROM speaker_profile_purity_tasks
        WHERE source_media_id=? AND start_ms=? AND end_ms=?""",
            (row["source_media_id"], row["start_ms"], row["end_ms"]),
        )
    }
    if task_ids != {r["task_id"] for r in references}:
        return SourcePurityEvidence(
            key, PurityVerdict.UNREVIEWED, None, row["evidence_id"]
        )
    for reference in references:
        latest = connection.execute(
            """SELECT review_id FROM speaker_profile_purity_reviews
            WHERE task_id=? ORDER BY revision DESC LIMIT 1""",
            (reference["task_id"],),
        ).fetchone()
        if (latest[0] if latest else None) != reference["review_id"]:
            return SourcePurityEvidence(
                key, PurityVerdict.UNREVIEWED, None, row["evidence_id"]
            )
    return SourcePurityEvidence(
        key,
        PurityVerdict(row["purity"]),
        row["review_primary_person_id"],
        row["evidence_id"],
        bool(row["conflicting"]),
    )


def reconcile(connection, now):
    tasks = [
        dict(r)
        for r in connection.execute("SELECT * FROM speaker_profile_purity_tasks")
    ]
    latest = {
        r["task_id"]: dict(r)
        for r in connection.execute("""
        SELECT r.* FROM speaker_profile_purity_reviews r WHERE NOT EXISTS
        (SELECT 1 FROM speaker_profile_purity_reviews n
         WHERE n.task_id=r.task_id AND n.revision>r.revision)""")
    }
    grouped = defaultdict(list)
    for task in tasks:
        key = ensure_source(
            connection, task["source_media_id"], task["start_ms"], task["end_ms"]
        )
        grouped[key].append(task)
        connection.execute(
            "INSERT OR IGNORE INTO speaker_purity_targets VALUES(?,?)",
            (key, task["target_person_id"]),
        )
    counts = Counter(
        {
            k: 0
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
    )
    details = []
    for key, sources in sorted(grouped.items()):
        references, signatures = [], set()
        for task in sources:
            review = latest.get(task["task_id"])
            references.append(
                {
                    "task_id": task["task_id"],
                    "target_person_id": task["target_person_id"],
                    "audit_run_id": task["audit_run_id"],
                    "review_id": review["review_id"] if review else None,
                    "review_revision": review["revision"] if review else None,
                    "review_source": review["review_source"] if review else None,
                    "reviewed_at": review["reviewed_at"] if review else None,
                    "action": review["action"] if review else None,
                    "purity": review["purity"] if review else None,
                    "primary_person_id": review["primary_speaker_person_id"]
                    if review
                    else None,
                }
            )
            # An undone/missing answer on a duplicate task cannot silently grant enrollment.
            signatures.add(
                (
                    review["purity"],
                    review["primary_speaker_person_id"],
                    review["primary_speaker_unknown"],
                )
                if review and review["action"] == "submit"
                else ("unreviewed", None, 1)
            )
        conflict = len(signatures) > 1
        purity, primary, unknown = (
            next(iter(signatures)) if not conflict else ("uncertain", None, 1)
        )
        if unknown and purity == "clean_single":
            purity = "uncertain"
        provenance = [
            {
                "task_id": s["task_id"],
                "source_session_id": s["source_session_id"],
                "source_track_id": s["source_track_id"],
                "source_cluster_id": s["source_cluster_id"],
                "snapshot": json.loads(s["source_snapshot_json"]),
            }
            for s in sources
        ]
        evidence_id = digest([key, references, provenance, purity, primary, conflict])
        connection.execute(
            "INSERT OR IGNORE INTO speaker_source_purity_evidence VALUES(?,?,?,?,?,?,?,?)",
            (
                evidence_id,
                key,
                purity,
                primary,
                int(conflict),
                json.dumps(references),
                json.dumps(provenance),
                now,
            ),
        )
        connection.execute(
            """INSERT INTO speaker_purity_current VALUES(?,?)
            ON CONFLICT(source_key) DO UPDATE SET evidence_id=excluded.evidence_id""",
            (key, evidence_id),
        )
        evidence = current_evidence(connection, key)
        targets = sorted({s["target_person_id"] for s in sources})
        contextual = {
            target: source_ineligibility(evidence, target, audio_available=True)
            for target in targets
        }
        reviewed = all(r["action"] == "submit" for r in references)
        counts["reviewed"] += reviewed
        if conflict:
            category = "conflicting"
        elif purity == "unreviewed":
            category = "unresolved"
        elif primary is not None and any(primary != t for t in targets):
            category = "wrong"
        else:
            category = {
                "clean_single": "clean",
                "mixed_overlap": "mixed",
                "boundary_cross": "boundary",
                "uncertain": "uncertain",
            }.get(purity, "unresolved")
        counts[category] += 1
        details.append(
            {
                "source_key": key,
                "evidence_id": evidence_id,
                "category": category,
                "targets": contextual,
                "review_references": references,
            }
        )
        connection.execute(
            """UPDATE purity_candidates SET status=?,updated_at=?
            WHERE source_key=? AND status!='superseded'""",
            (
                "unreviewed"
                if conflict or purity == "unreviewed"
                else "reviewed_rejected",
                now,
                key,
            ),
        )
        for target, reason in contextual.items():
            if reason is None:
                connection.execute(
                    """UPDATE purity_candidates SET status='reviewed_clean'
                    WHERE source_key=? AND target_person_id=? AND status!='superseded'""",
                    (key, target),
                )
    return dict(counts) | {
        "task_count": len(tasks),
        "unique_sources": len(grouped),
        "duplicate_tasks": len(tasks) - len(grouped),
        "sources": details,
    }


def register_candidates(connection, plan, now, *, capacity=5):
    """Only worker-selected windows, bounded per person/session; no review tasks.

    Dates/acoustic proxies are not inferred without evidence. New sessions and
    clean coverage gaps rank first. Old selections become superseded.
    """
    session = plan.track.session_id
    connection.execute(
        """UPDATE purity_candidates SET status='superseded',updated_at=?
        WHERE target_person_id=? AND source_session_id=? AND candidate_kind='enrollment'""",
        (now, plan.person_id, session),
    )
    clean_count = connection.execute(
        """SELECT COUNT(*) FROM speaker_purity_targets t
        JOIN speaker_purity_current c USING(source_key)
        JOIN speaker_source_purity_evidence e USING(evidence_id)
        WHERE t.target_person_id=? AND e.purity='clean_single' AND e.conflicting=0
          AND e.review_primary_person_id=t.target_person_id""",
        (plan.person_id,),
    ).fetchone()[0]
    sessions = {
        r[0]
        for r in connection.execute(
            """SELECT DISTINCT task.source_session_id
        FROM speaker_profile_purity_tasks task JOIN speaker_purity_current c USING(source_key)
        JOIN speaker_source_purity_evidence e USING(evidence_id)
        WHERE task.target_person_id=? AND e.purity='clean_single' AND e.conflicting=0
          AND e.review_primary_person_id=task.target_person_id""",
            (plan.person_id,),
        )
    }
    # Longest first avoids asking for one unusably short clip in a new session.
    proposed_duration, proposed_count = 0, 0
    ranked = sorted(
        plan.windows,
        key=lambda w: (-(w["end_ms"] - w["start_ms"]), w["media_id"], w["start_ms"]),
    )
    for index, window in enumerate(ranked):
        key = ensure_source(
            connection, window["media_id"], window["start_ms"], window["end_ms"]
        )
        connection.execute(
            "INSERT OR IGNORE INTO speaker_purity_targets VALUES(?,?)",
            (key, plan.person_id),
        )
        evidence = current_evidence(connection, key)
        reason = source_ineligibility(evidence, plan.person_id, audio_available=True)
        status = (
            "reviewed_clean"
            if reason is None
            else "unreviewed"
            if reason in ("unreviewed", "conflicting_review")
            else "reviewed_rejected"
        )
        if status == "unreviewed":
            if proposed_count >= capacity or (
                clean_count >= capacity and proposed_duration >= 6000
            ):
                status = "not_needed"
            else:
                proposed_count += 1
                proposed_duration += window["end_ms"] - window["start_ms"]
        provenance = json.dumps(
            {
                "sample_key": plan.key,
                "fact_ids": plan.fact_ids,
                "track_id": plan.track.speaker_track_id,
                "cluster_id": plan.cluster_id,
                "coverage_gap": clean_count < capacity,
                "new_session": session not in sessions,
                "minimum_duration_basis": "default existing quality 0.5 * 12000ms",
            }
        )
        connection.execute(
            """INSERT INTO purity_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_key,target_person_id) DO UPDATE SET status=excluded.status,
            risk_rank=excluded.risk_rank,provenance_json=excluded.provenance_json,
            updated_at=excluded.updated_at""",
            (
                digest([key, plan.person_id]),
                key,
                plan.person_id,
                session,
                "enrollment",
                None,
                status,
                (100 if clean_count < capacity else 50)
                + (40 if session not in sessions else 0)
                - index,
                provenance,
                now,
                now,
            ),
        )


def propose_recrop(
    connection, parent_key, target, media_id, start_ms, end_ms, now, provenance
):
    parent = connection.execute(
        "SELECT * FROM speaker_purity_sources WHERE source_key=?", (parent_key,)
    ).fetchone()
    if (
        parent is None
        or parent["source_media_id"] != media_id
        or not (parent["start_ms"] <= start_ms < end_ms <= parent["end_ms"])
    ):
        raise ValueError("recrop must be contained within the original source")
    key = ensure_source(connection, media_id, start_ms, end_ms)
    if key == parent_key:
        raise ValueError("recrop must have a new, smaller range")
    # Never inherit the parent verdict, even when parent is clean.
    connection.execute(
        "INSERT OR IGNORE INTO speaker_purity_targets VALUES(?,?)", (key, target)
    )
    connection.execute(
        "INSERT OR IGNORE INTO purity_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            digest([key, target]),
            key,
            target,
            provenance.get("session_id"),
            "recrop_candidate",
            parent_key,
            "unreviewed",
            100,
            json.dumps(provenance),
            now,
            now,
        ),
    )
    return key
