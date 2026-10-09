"""SQLite inventory of daily source metadata; no transcript payloads or model calls."""

import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.adapters.sqlite.sound_eligibility import usable_content
from allday_asr.v3.application.daily_inventory import instant, classify_day, BACKFILL_CLASSES

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

    def scan(self, *, include_today=False, only_dates=None):
        started = time.perf_counter()
        zone = ZoneInfo(self.timezone)
        today = self.now().astimezone(zone).date()
        last = today if include_today else today - timedelta(days=1)
        if only_dates is not None:
            dates = sorted({datetime.fromisoformat(value).date() for value in only_dates
                            if datetime.fromisoformat(value).date() <= last})
            if len(dates) > 1:
                return [item for day in dates for item in
                        self.scan(include_today=include_today, only_dates=(day.isoformat(),))]
            if not dates:
                return []
            target_day = dates[0]
            start = datetime.combine(target_day, datetime.min.time(), zone).astimezone(timezone.utc)
            end = (datetime.combine(target_day, datetime.min.time(), zone) +
                   timedelta(days=1)).astimezone(timezone.utc)
            start_text = start.isoformat().replace('+00:00', 'Z')
            end_text = end.isoformat().replace('+00:00', 'Z')
            session_scope = " AND julianday(s.captured_start)<julianday(?) AND " \
                "(s.captured_end IS NULL OR julianday(s.captured_end)>julianday(?))"
            source_scope = " AND julianday(u.start_at)>=julianday(?) AND julianday(u.start_at)<julianday(?)"
            session_args = (end_text, start_text)
            source_args = (start_text, end_text)
        else:
            target_day = None
            session_scope = source_scope = ""
            session_args = source_args = ()
        date_text = target_day.isoformat() if target_day else None
        summary_scope = " AND d.summary_date=?" if date_text else ""
        event_scope = " AND json_extract(payload_json,'$.local_date')=?" if date_text else ""
        job_scope = " WHERE local_date=?" if date_text else ""
        with self.database.read() as c:
            sessions = [
                dict(r)
                for r in c.execute(f"""SELECT s.session_id,s.captured_start,
                s.captured_end,s.status_code,s.state,
                (SELECT p.status FROM processing_runs p WHERE p.session_id=s.session_id
                 ORDER BY p.created_at DESC,p.run_id DESC LIMIT 1) AS run_status,
                (SELECT m.sha256 FROM session_manifests m WHERE m.session_id=s.session_id
                 ORDER BY m.created_at DESC,m.manifest_id DESC LIMIT 1) AS manifest_sha
                FROM recording_sessions s WHERE s.tombstoned_at IS NULL{session_scope}""", session_args)
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
                WHERE u.status='active' AND trim(u.text)!='' AND {usable_content("u")}{source_scope}
                ORDER BY u.start_at,u.utterance_id,m.cluster_id,pl.person_id""", source_args)
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
                  {summary_scope}
                  AND d.revision=(SELECT MAX(latest.revision) FROM daily_summary_revisions latest
                    WHERE latest.summary_id=d.summary_id)""".format(summary_scope=summary_scope),
                (self.timezone, date_text) if date_text else (self.timezone,),
            ).fetchall()
            summaries = {r["summary_date"]: dict(r) for r in summary_rows}
            events = {
                r["event_id"]: dict(r)
                for r in c.execute("""SELECT event_id,revision,status,
                json_extract(payload_json,'$.local_date') AS date FROM event_current_states
                WHERE json_extract(payload_json,'$.daily_event_version') IS NOT NULL
                {event_scope}""".format(event_scope=event_scope),
                (date_text,) if date_text else ())
            }
            table = c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='daily_generation_jobs'"
            ).fetchone()
            jobs = (
                [dict(r) for r in c.execute(
                    f"SELECT * FROM daily_generation_jobs{job_scope}",
                    (date_text,) if date_text else ())]
                if table
                else []
            )
            # These small source descriptors avoid loading transcript or model state.
            audio = [
                tuple(r)
                for r in c.execute(f"""SELECT capture_segments.session_id,asset_id,sequence,
                    session_start_ms,session_end_ms,source_start_ms FROM capture_segments
                    JOIN recording_sessions s ON s.session_id=capture_segments.session_id
                    WHERE s.tombstoned_at IS NULL{session_scope}
                    ORDER BY capture_segments.session_id,sequence""", session_args)
            ]
            tasks = [
                dict(r)
                for r in c.execute(f"""SELECT e.event_id,e.revision,e.status,l.evidence_id,u.start_at
                FROM event_current_states e JOIN evidence_links l ON l.subject_type='event'
                AND l.subject_id=e.event_id AND l.subject_revision=e.revision AND l.evidence_type='utterance'
                JOIN utterances u ON u.utterance_id=l.evidence_id
                WHERE e.event_kind IN ('task','request','appointment','commitment')
                AND json_extract(e.payload_json,'$.daily_event_version') IS NULL
                {source_scope}
                ORDER BY e.event_id,l.evidence_id""", source_args)
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
            first = max(first, target_day) if target_day else first
            finish = min(finish, target_day) if target_day else finish
            if finish < first:
                continue
            for offset in range(max(0, (min(finish, last) - first).days) + 1):
                session_days.setdefault(
                    (first + timedelta(days=offset)).isoformat(), []
                ).append(session)
        keys = [
            d for d in set(source_days) | set(session_days)
            if d <= last.isoformat() and (target_day is None or d == target_day.isoformat())
        ]
        self.last_scan_stats = {
            "sessions": len(sessions), "utterance_rows": len(sources),
            "summaries": len(summaries), "events": len(events),
            "jobs": len(jobs), "audio_rows": len(audio), "task_rows": len(tasks),
            "elapsed_ms": 0.0,
        }
        if not keys:
            self.last_scan_stats["elapsed_ms"] = (time.perf_counter() - started) * 1000
            return []
        current = target_day if target_day else datetime.fromisoformat(min(keys)).date()
        last = target_day if target_day else last
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
            # A configuration upgrade describes a new producer; it is not new evidence.
            same_sources = any(j["source_fingerprint"] == digest for j in prior_versions)
            published_without_queue = bool(
                summary and not prior_versions and summary.get("semantic_status") == "complete"
                and not any(instant(r["updated_at"]) > instant(summary["created_at"]) for r in rows))
            config_only = bool(
                not complete and not changed and not matched and
                (same_sources or published_without_queue))
            item = {
                "automatic_recompute_blocked": config_only,
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
        self.last_scan_stats["elapsed_ms"] = (time.perf_counter() - started) * 1000
        return result
