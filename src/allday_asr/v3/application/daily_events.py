"""Incremental daily projection over the shared V3 event and summary stores."""
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
from .daily_structured_summary import persist_summary
from .insight_projections import _date, _period


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
                self.refresh_daily(day)
            self._daily_refreshed_at = time.monotonic()

    def refresh_daily(self, summary_date, timezone_name: str = 'Asia/Singapore') -> dict:
        day = _date(summary_date)
        start, end = _period(day, timezone_name)
        now = self._now()
        # One transaction makes source snapshots, CAS event revisions, summary and sync atomic.
        with self._uow_factory() as uow:
            rows, tasks = daily_inputs(uow, start.isoformat(), end.isoformat())
            existing = [uow.knowledge.get_event(v['event_id'])
                        for v in uow.insights.daily_event_states(day.isoformat(), timezone_name)]
            used = set()
            states = []
            for candidate in normalize_candidates(candidate_windows(rows)):
                if not meaningful_event(candidate):
                    continue
                payload = event_payload(candidate, day.isoformat(), timezone_name)
                evidence_ids = {r['utterance_id'] for r in candidate['evidence']}
                choices = [s for s in existing if s.event_id not in used and
                           (evidence_ids & {r['utterance_id'] for r in s.payload['evidence_snapshots']}
                            or _source_overlap(candidate['evidence'], s.payload['evidence_snapshots']))]
                # Keep the oldest identity when two previous events merge; split gets new IDs.
                current = min(choices, key=lambda s: (
                    -len(evidence_ids & {r['utterance_id'] for r in s.payload['evidence_snapshots']}),
                    s.created_at, s.event_id)) if choices else None
                eid = current.event_id if current else stable_ulid(
                    'daily-event', timezone_name, day.isoformat(), candidate['evidence'][0]['utterance_id'])
                if not current:
                    current = uow.knowledge.get_event(eid)
                if current and current.payload.get('manual_fixed'):
                    # Preserve explicit user fixation; stale derivation remains visible for review.
                    state = current
                elif (current and current.payload == payload and current.status == EventStatus.ACTIVE
                      and current.derivation_status == 'active'):
                    state = current
                else:
                    state = persist_event(uow, eid, payload, current, now)
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
            return persist_summary(uow, active, tasks, day.isoformat(), timezone_name, start, end, now)


def _source_overlap(current: list[dict], previous: list[dict]) -> bool:
    # Full ASR reruns may replace utterance IDs. Reuse identity through physical source ranges.
    return any(a['session_id'] == b['session_id'] and a['start_ms'] < b['end_ms']
               and b['start_ms'] < a['end_ms'] and seconds(a['start_at']) < seconds(b['end_at'])
               and seconds(b['start_at']) < seconds(a['end_at']) for a in current for b in previous)
