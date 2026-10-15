"""Incremental daily projection over the shared V3 event and summary stores."""
import logging
from .failure_diagnostics import log_failure, failure_category
from datetime import datetime
from zoneinfo import ZoneInfo
import time
from allday_asr.v3.domain.daily_events import (
    candidate_windows, normalize_candidates, event_payload, meaningful_event, seconds,
)
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.knowledge import EventStatus
from .daily_event_inputs import daily_inputs
from .daily_event_persistence import persist_event
from .daily_structured_summary import persist_summary, select_major
from .daily_overview import prepare_overview
from .daily_event_persistence import event_resource
from types import SimpleNamespace
from .insight_projections import _date, _period
from .daily_semantic_analysis import analyze_daily
from allday_asr.v3.domain.daily_semantics import semantic_payload
from allday_asr.v3.domain.hashing import canonical_json_sha256


class DailyEventMixin:
    def refresh_daily_cache(self, force: bool = False) -> None:
        # Serialize refreshes and rate-limit repeat sync pages. Explicit day refresh is immediate.
        with self._daily_refresh_lock:
            if not force and time.monotonic() - self._daily_refreshed_at < 30:
                return
            with self._uow_factory().reading() as uow:
                starts = uow.insights.daily_source_starts()
                existing = uow.insights.list_daily(366)
            zone = ZoneInfo('Asia/Singapore')
            days = {datetime.fromisoformat(s.replace('Z', '+00:00')).astimezone(zone).date().isoformat()
                    for s in starts}
            days.update(s['summary_date'] for s in existing if s['timezone'] == 'Asia/Singapore')
            for day in sorted(days):
                self.refresh_daily(day, allow_model=False)
            self._daily_refreshed_at = time.monotonic()

    def mark_daily_stale(self, day, timezone_name, fingerprint, *, expected_revision=None):
        from allday_asr.v3.domain.knowledge import InvalidationEvent
        sid = stable_ulid('daily-summary', day, timezone_name)
        with self._uow_factory() as uow:
            try:
                summary = uow.insights.daily(sid)
            except KeyError:
                return False
            if expected_revision is not None and summary['revision'] != expected_revision:
                from .generation_inputs import GenerationSourceConflict
                raise GenerationSourceConflict('daily content changed before local invalidation; retry')
            identifier = stable_ulid('daily-pending', sid, summary['revision'], fingerprint)
            uow.derivations.add_invalidation(InvalidationEvent(
                identifier, 'daily_summary', sid, summary['revision'], 'stale',
                'daily_sources_pending_regeneration', 'daily_sources', fingerprint, 1,
                identifier, self._now()))
            from .daily_sync_projection import publish_daily_summary
            return publish_daily_summary(uow, uow.insights.daily(sid))

    def daily_sync_differences(self, first_date, last_date, timezone_name='Asia/Singapore'):
        from .daily_sync_projection import daily_summary_resource
        differences = []
        with self._uow_factory().reading() as uow:
            for day in _repair_dates(first_date, last_date):
                sid = stable_ulid('daily-summary', day, timezone_name)
                try:
                    summary = uow.insights.daily(sid)
                except KeyError:
                    continue
                latest = uow.changes.latest('daily_summary', sid)
                if summary['derivation_status'] == 'stale' and (latest is None or latest.payload != daily_summary_resource(summary)):
                    differences.append({'summary_id': sid, 'date': day, 'revision': summary['revision'],
                                        'derivation_status': 'stale', 'latest_sequence': latest.sequence if latest else None})
        return differences

    def repair_daily_sync(self, first_date, last_date, *, actor, timezone_name='Asia/Singapore'):
        from .daily_sync_projection import publish_daily_summary
        if not actor.strip():
            raise ValueError('daily sync repair requires an actor')
        repaired = []
        for day in _repair_dates(first_date, last_date):
            sid = stable_ulid('daily-summary', day, timezone_name)
            with self._uow_factory() as uow:
                try:
                    summary = uow.insights.daily(sid)
                except KeyError:
                    continue
                if summary['derivation_status'] == 'stale' and publish_daily_summary(uow, summary, actor=actor):
                    repaired.append({'summary_id': sid, 'revision': summary['revision'], 'date': day})
        return {'repaired_count': len(repaired), 'repaired': repaired}

    def refresh_daily(self, summary_date, timezone_name: str = 'Asia/Singapore', *, allow_model=True) -> dict:
        day = _date(summary_date)
        start, end = _period(day, timezone_name)
        now = self._now()
        # Inference never holds a writer or stalls a mobile sync request.
        with self._uow_factory().reading() as uow:
            rows, tasks = daily_inputs(uow, start.isoformat(), end.isoformat())
            initial_existing = [uow.knowledge.get_event(v['event_id'])
                                for v in uow.insights.daily_event_states(day.isoformat(), timezone_name)]
            existing_hash = canonical_json_sha256([event_resource(s) for s in initial_existing])
        source_hash = canonical_json_sha256({'rows':rows,'tasks':tasks})
        try:
            semantic, semantic_error = analyze_daily(self, rows, allow_model=allow_model)
        except Exception as exc:
            log_failure(logging.getLogger(__name__), exc, date=day.isoformat(), stage="semantic")
            # Keep published events/summary unchanged on provider or validation failure.
            return {'semantic_status':'retryable' if failure_category(exc)[1] else 'failed', 'error':'semantic_generation_failed', 'error_category':failure_category(exc)[0], 'retryable':failure_category(exc)[1]}
        if semantic is None and not allow_model:
            return {'semantic_status':'pending','error':semantic_error}
        candidates = semantic if semantic is not None else normalize_candidates(candidate_windows(rows))
        try:
            with self._uow_factory().reading() as uow:
                plans, preview = _plan_events(uow, candidates, initial_existing, rows, semantic,
                                              day.isoformat(), timezone_name, now)
            major = select_major([event_resource(s) for s in preview if s.derivation_status == 'active'])
            synthesis, overview_error = prepare_overview(self, major, tasks, day.isoformat(), allow_model=allow_model)
            if overview_error:
                return {'semantic_status':'pending','error':overview_error}
        except Exception as exc:
            log_failure(logging.getLogger(__name__), exc, date=day.isoformat(), stage='overview')
            return {'semantic_status':'retryable' if failure_category(exc)[1] else 'failed','error':'semantic_overview_failed', 'error_category':failure_category(exc)[0], 'retryable':failure_category(exc)[1]}
        # One transaction makes source snapshots, CAS event revisions, summary and sync atomic.
        with self._uow_factory() as uow:
            fresh_rows, fresh_tasks = daily_inputs(uow, start.isoformat(), end.isoformat())
            if canonical_json_sha256({'rows':fresh_rows,'tasks':fresh_tasks})!=source_hash:
                return {'semantic_status':'retryable','error':'daily_sources_changed'}
            existing = [uow.knowledge.get_event(v['event_id'])
                        for v in uow.insights.daily_event_states(day.isoformat(), timezone_name)]
            if canonical_json_sha256([event_resource(s) for s in existing]) != existing_hash:
                return {'semantic_status':'retryable','error':'daily_events_changed'}
            used, states = set(), []
            for eid, payload, current, preview in plans:
                unchanged = current is preview
                state = current if unchanged else persist_event(uow, eid, payload, current, now)
                used.add(eid)
                states.append(state)
            for state in existing:
                if state.event_id not in used and state.status == EventStatus.ACTIVE:
                    if state.payload.get('manual_fixed'):
                        states.append(state)
                    else:
                        persist_event(uow, state.event_id,
                                      {**state.payload, 'retirement_reason': 'evidence_reconciled'},
                                      state, now, retire=True)
            # Never summarize stale fixed evidence as current fact.
            active = [s for s in states if s.derivation_status == 'active']
            return persist_summary(uow, active, tasks, day.isoformat(), timezone_name, start, end, now,
                                   semantic_error=semantic_error, synthesis=synthesis)


def _repair_dates(first_date, last_date):
    from datetime import timedelta
    first, last = _date(first_date), _date(last_date)
    if not 0 <= (last - first).days < 14:
        raise ValueError('explicit daily sync repair range must contain 1 to 14 days')
    return tuple((first + timedelta(days=i)).isoformat() for i in range((last-first).days + 1))


def _source_overlap(current: list[dict], previous: list[dict]) -> bool:
    # Full ASR reruns may replace utterance IDs. Reuse identity through physical source ranges.
    return any(a['session_id'] == b['session_id'] and a['start_ms'] < b['end_ms']
               and b['start_ms'] < a['end_ms'] and seconds(a['start_at']) < seconds(b['end_at'])
               and seconds(b['start_at']) < seconds(a['end_at']) for a in current for b in previous)


def _plan_events(uow, candidates, existing, rows, semantic, day, timezone_name, now):
    used = set()
    current_source_ids = {r['utterance_id'] for r in rows}
    plans, states = [], []
    for candidate in candidates:
        if semantic is None and not meaningful_event(candidate):
            continue
        payload = (semantic_payload(candidate,day,timezone_name) if semantic is not None
                   else event_payload(candidate, day, timezone_name))
        evidence_ids = {r['utterance_id'] for r in candidate['evidence']}
        choices = [s for s in existing if s.event_id not in used and
                   (evidence_ids & {r['utterance_id'] for r in s.payload['evidence_snapshots']}
                    or (not current_source_ids & {r['utterance_id'] for r in s.payload['evidence_snapshots']}
                        and _source_overlap(candidate['evidence'], s.payload['evidence_snapshots'])))]
        # Keep the oldest identity when two previous events merge; split gets new IDs.
        current = min(choices, key=lambda s: (
            -len(evidence_ids & {r['utterance_id'] for r in s.payload['evidence_snapshots']}),
            s.created_at, s.event_id)) if choices else None
        eid = current.event_id if current else stable_ulid(
            'daily-event', timezone_name, day, candidate['evidence'][0]['utterance_id'])
        if eid in used:
            # A split may reuse the old anchor ID for an earlier branch.
            # Give the other branch a deterministic distinct identity.
            eid = stable_ulid('daily-event-split', timezone_name, day,
                              candidate['evidence'][0]['utterance_id'])
        if eid in used:
            raise ValueError('daily event identity collision; retry required')
        if not current:
            current = uow.knowledge.get_event(eid)
        if current and current.payload.get('manual_fixed'):
            # Preserve explicit user fixation; stale derivation remains visible for review.
            state = current
        elif (current and current.payload == payload and current.status == EventStatus.ACTIVE
              and current.derivation_status == 'active'):
            state = current
        else:
            state = SimpleNamespace(event_id=eid, payload=payload,
                revision=current.revision + 1 if current else 1,
                status=EventStatus.ACTIVE, derivation_status='active',
                created_at=current.created_at if current else now, updated_at=now)
        plans.append((eid, payload, current, state))
        used.add(eid)
        states.append(state)
    states.extend(s for s in existing if s.event_id not in used and s.status == EventStatus.ACTIVE
                  and s.payload.get('manual_fixed'))
    return plans, states
