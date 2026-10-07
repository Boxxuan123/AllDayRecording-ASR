"""Read-only source metadata inventory, without model calls or transcript loads."""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.adapters.sqlite.sound_eligibility import usable_content

BACKFILL_CLASSES = {"LEGACY_ONLY", "MISSING", "STALE_SOURCE", "FAILED_RESUMABLE"}


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def generation_version(analyzer):
    from .daily_generation_reliability import GENERATION_POLICY_VERSION
    from allday_asr.v3.domain.daily_protocol import CANONICALIZATION_VERSION

    names = (
        "model_label",
        "provider",
        "producer_version",
        "reasoning_effort",
        "prompt_version",
        "extractor_version",
        "reconcile_prompt_version",
        "reconcile_schema_version",
        "normalization_prompt_version",
        "normalization_review_prompt_version",
        "normalization_scope_prompt_version",
        "overview_prompt_version",
        "overview_schema_version",
    )
    return canonical_json_sha256(
        {
            "policy": GENERATION_POLICY_VERSION,
            "canonicalization": CANONICALIZATION_VERSION,
            **{k: getattr(analyzer, k, None) for k in names},
        }
    )


def classify_day(item, job, *, current_product, source_changed, today):
    if not item["upstream_complete"]:
        return "UPSTREAM_INCOMPLETE", "Recording/ASR input is not complete"
    if not item["active_utterance_count"]:
        return "NO_SOURCE", "No eligible active utterances"
    if item["date"] >= today:
        return "STALE_SOURCE", "Current day stays dirty until local rollover"
    if job and job["status"] == "FAILED_TERMINAL":
        return "FAILED_TERMINAL", job.get(
            "error"
        ) or "Persistent retry budget exhausted"
    if job and job["status"] == "WAITING_PROVIDER":
        return (
            "UNKNOWN",
            "NOT_ATTEMPTED_PROVIDER_OUTAGE; held until explicit retry or source/version change",
        )
    if job and job["status"] in ("FAILED_RETRYABLE", "RUNNING", "PENDING"):
        return (
            "FAILED_RESUMABLE",
            "Persisted job/checkpoints; source identity unchanged",
        )
    if current_product and not source_changed:
        kind = (
            "CURRENT_COMPLETE"
            if item["existing_final_event_count"]
            else "CURRENT_COMPLETE_EMPTY"
        )
        return (
            kind,
            "Complete approved product, source metadata and published revisions match",
        )
    if current_product:
        return "STALE_SOURCE", "Source fingerprint/revision changed after publication"
    if item["existing_summary_present"] or item["existing_final_event_count"]:
        return (
            "LEGACY_ONLY",
            "Published row is not an approved complete semantic generation",
        )
    return "MISSING", "Eligible sources have no approved published generation"


class DailyHistoryInventory:
    def __init__(
        self,
        database,
        version,
        *,
        model="gpt-5.6-luna",
        timezone_name="Asia/Singapore",
        now=None,
    ):
        self.database, self.version, self.model = database, version, model
        self.timezone = timezone_name
        self.now = now or (lambda: datetime.now(timezone.utc))

    def scan(self, *, include_today=False):
        zone = ZoneInfo(self.timezone)
        today = self.now().astimezone(zone).date()
        last = today if include_today else today - timedelta(days=1)
        with self.database.read() as c:
            sessions = [
                dict(r)
                for r in c.execute("""SELECT s.session_id,s.captured_start,
                s.captured_end,s.status_code,s.state,
                (SELECT p.status FROM processing_runs p WHERE p.session_id=s.session_id
                 ORDER BY p.created_at DESC,p.run_id DESC LIMIT 1) AS run_status,
                (SELECT m.sha256 FROM session_manifests m WHERE m.session_id=s.session_id
                 ORDER BY m.created_at DESC,m.manifest_id DESC LIMIT 1) AS manifest_sha
                FROM recording_sessions s WHERE s.tombstoned_at IS NULL""")
            ]
            sources = [
                dict(r)
                for r in c.execute(f"""SELECT u.utterance_id,u.revision,
                u.session_id,u.run_id,u.start_at,u.end_at,u.start_ms,u.end_ms,
                u.speaker_track_id,u.identity,u.updated_at,
                m.cluster_id,pl.person_id,p.revision AS person_revision
                FROM utterances u
                LEFT JOIN speaker_cluster_memberships m ON m.speaker_track_id=u.speaker_track_id AND m.state='active'
                LEFT JOIN person_cluster_links pl ON pl.cluster_id=m.cluster_id AND pl.status='active'
                LEFT JOIN persons p ON p.person_id=pl.person_id
                WHERE u.status='active' AND trim(u.text)!='' AND {usable_content("u")}
                ORDER BY u.start_at,u.utterance_id,m.cluster_id,pl.person_id""")
            ]
            summary_rows = c.execute(
                """SELECT d.summary_id,d.revision,d.summary_date,d.created_at,
                json_extract(d.objective_json,'$.generation_version') AS version,
                json_extract(d.objective_json,'$.semantic_status') AS semantic_status,
                json_extract(d.objective_json,'$.source_event_revisions') AS event_revisions,
                json_extract(d.provenance_json,'$.overview_synthesis.model') AS model,
                json_extract(d.provenance_json,'$.overview_synthesis.prompt_version') AS overview_prompt,
                json_type(d.objective_json,'$.major_events') AS major_events_type,
                json_array_length(d.objective_json,'$.major_events') AS major_count,
                json_extract(d.provenance_json,'$.overview_synthesis.provider') AS overview_provider,
                json_type(d.provenance_json,'$.overview_synthesis.remote') AS overview_remote_type,
                json_extract(d.objective_json,'$.statistics.event_count') AS final_count,
                EXISTS(SELECT 1 FROM invalidation_events i WHERE i.target_type='daily_summary'
                  AND i.target_id=d.summary_id AND i.target_revision=d.revision
                  AND i.status IN ('stale','invalid')) AS invalidated
                FROM daily_summary_revisions d WHERE d.timezone=? AND d.status='active'
                  AND d.revision=(SELECT MAX(latest.revision) FROM daily_summary_revisions latest
                    WHERE latest.summary_id=d.summary_id)""",
                (self.timezone,),
            ).fetchall()
            summaries = {r["summary_date"]: dict(r) for r in summary_rows}
            events = {
                r["event_id"]: dict(r)
                for r in c.execute("""SELECT event_id,revision,status,
                json_extract(payload_json,'$.local_date') AS date FROM event_current_states
                WHERE json_extract(payload_json,'$.daily_event_version') IS NOT NULL""")
            }
            table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='daily_generation_jobs'"
            ).fetchone()
            jobs = (
                [dict(r) for r in c.execute("SELECT * FROM daily_generation_jobs")]
                if table
                else []
            )
            # These small source descriptors avoid loading transcript or model state.
            audio = [
                tuple(r)
                for r in c.execute(
                    "SELECT session_id,asset_id,sequence,session_start_ms,session_end_ms,source_start_ms FROM capture_segments ORDER BY session_id,sequence"
                )
            ]
            tasks = [
                dict(r)
                for r in c.execute("""SELECT e.event_id,e.revision,e.status,l.evidence_id,u.start_at
                FROM event_current_states e JOIN evidence_links l ON l.subject_type='event'
                AND l.subject_id=e.event_id AND l.subject_revision=e.revision AND l.evidence_type='utterance'
                JOIN utterances u ON u.utterance_id=l.evidence_id
                WHERE e.event_kind IN ('task','request','appointment','commitment')
                AND json_extract(e.payload_json,'$.daily_event_version') IS NULL
                ORDER BY e.event_id,l.evidence_id""")
            ]
        source_days = {}
        for row in sources:
            day = instant(row["start_at"]).astimezone(zone).date().isoformat()
            source_days.setdefault(day, []).append(row)
        session_days = {}
        for session in sessions:
            first = instant(session["captured_start"]).astimezone(zone).date()
            end = (
                instant(session["captured_end"])
                if session["captured_end"]
                else self.now()
            )
            finish = (end - timedelta(microseconds=1)).astimezone(zone).date()
            for offset in range(max(0, (min(finish, last) - first).days) + 1):
                session_days.setdefault(
                    (first + timedelta(days=offset)).isoformat(), []
                ).append(session)
        keys = [
            d for d in set(source_days) | set(session_days) if d <= last.isoformat()
        ]
        if not keys:
            return []
        current = datetime.fromisoformat(min(keys)).date()
        result = []
        while current <= last:
            day = current.isoformat()
            rows = source_days.get(day, [])
            ss = session_days.get(day, [])
            ids = {s["session_id"] for s in ss} | {r["session_id"] for r in rows}
            digest = canonical_json_sha256(
                {
                    "source_metadata": rows,
                    "manifests": sorted(
                        (s["session_id"], s["manifest_sha"]) for s in ss
                    ),
                    "audio": [a for a in audio if a[0] in ids],
                    "tasks": [
                        t
                        for t in tasks
                        if instant(t["start_at"]).astimezone(zone).date().isoformat()
                        == day
                    ],
                }
            )
            summary = summaries.get(day, {})
            matched = next(
                (
                    j
                    for j in jobs
                    if j["local_date"] == day
                    and j["timezone"] == self.timezone
                    and j["source_fingerprint"] == digest
                    and j["generation_version"] == self.version
                ),
                None,
            )
            upstream = all(
                s["captured_end"]
                and s["state"]
                not in ("capturing", "closing", "recovering", "quarantined")
                and s["run_status"] == "succeeded"
                and s["status_code"] in ("ready", "available")
                for s in ss
            )
            revisions = json.loads(summary.get("event_revisions") or "{}")
            consistent = all(
                eid in events
                and events[eid]["revision"] == rev
                and events[eid]["status"] == "active"
                for eid, rev in revisions.items()
            )
            complete = bool(
                summary.get("version") == SEMANTIC_VERSION
                and summary.get("semantic_status") == "complete"
                and (
                    not summary.get("final_count")
                    # The approved writer skips model overview when no major
                    # events exist, even when secondary Final events remain.
                    or (
                        summary.get("major_events_type") == "array"
                        and summary.get("major_count") == 0
                        and summary.get("overview_provider") == "local"
                        and summary.get("overview_remote_type") == "false"
                    )
                    or (
                        summary.get("model") == self.model
                        and summary.get("overview_prompt")
                        == "daily-semantic-v1.2-overview-prompt.1"
                    )
                )
            )
            prior_versions = [
                j
                for j in jobs
                if j["local_date"] == day and j["timezone"] == self.timezone
            ]
            if prior_versions and not any(
                j["generation_version"] == self.version for j in prior_versions
            ):
                complete = (
                    False  # A new approved generation configuration is a real upgrade.
                )
            if matched and matched["status"] == "SUCCEEDED":
                changed = matched["summary_id"] != summary.get("summary_id") or matched[
                    "summary_revision"
                ] != summary.get("revision")
            else:
                # Existing approved publication predates the queue. Its original CAS
                # used all sources; no source may have been revised since that start.
                changed = (
                    any(
                        instant(r["updated_at"]) > instant(summary["created_at"])
                        for r in rows
                    )
                    if complete
                    else False
                )
                changed = changed or any(
                    j["local_date"] == day
                    and j["timezone"] == self.timezone
                    and j["generation_version"] == self.version
                    and j["status"] == "SUCCEEDED"
                    and j["source_fingerprint"] != digest
                    for j in jobs
                )
            changed = changed or not consistent or bool(summary.get("invalidated"))
            item = {
                "date": day,
                "timezone": self.timezone,
                "session_count": len(ss),
                "active_utterance_count": len({r["utterance_id"] for r in rows}),
                "source_fingerprint": digest,
                "upstream_complete": bool(upstream),
                "existing_generation_version": summary.get("version"),
                "existing_generation_status": summary.get("semantic_status"),
                "existing_final_event_count": summary.get("final_count") or 0,
                "existing_summary_present": bool(summary),
                "summary_id": summary.get("summary_id"),
                "summary_revision": summary.get("revision"),
                "generation_version": self.version,
                "queue_state": matched["status"] if matched else None,
                "attempts": matched["attempts"] if matched else 0,
            }
            classification, reason = classify_day(
                item,
                matched,
                current_product=complete,
                source_changed=changed,
                today=today.isoformat(),
            )
            item.update(
                classification=classification,
                reason=reason,
                needs_backfill=classification in BACKFILL_CLASSES
                and day < today.isoformat(),
            )
            result.append(item)
            current += timedelta(days=1)
        return result
