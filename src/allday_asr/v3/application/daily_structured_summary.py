"""Structured summaries contain only source-linked facts and current task state."""
from datetime import datetime
from zoneinfo import ZoneInfo
from allday_asr.v3.domain.daily_semantics import SEMANTIC_VERSION
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.knowledge import KnowledgeLayer
from .daily_event_persistence import local_generation, event_resource
from .insight_validation import _link_evidence


def structured_summary(events, tasks: tuple[dict, ...], day: str) -> dict:
    sources = [event_resource(state) for state in events]
    links = {t['event_id']: [e['event_id'] for e in sources if t['event_id'] in e['linked_task_ids']]
             for t in tasks}
    task_items = [{'task_id': t['event_id'], 'title': t['payload'].get('title', '待办'),
                   'status': t['status'], 'revision': t['revision'],
                   'scheduled_at': t.get('scheduled_at') or t['payload'].get('scheduled_at'),
                   'created_at': t['created_at'], 'completed_at': t.get('completed_at'),
                   'event_ids': links[t['event_id']]}
                  for t in tasks if links[t['event_id']]]
    people = {}
    for event in sources:
        for participant in event['participants']:
            if participant['kind'] == 'self':
                continue
            key = participant['key']
            item = people.setdefault(key, {**participant, 'event_ids': []})
            item['event_ids'].append(event['event_id'])
    major = [e for e in sources if e.get('summary_visibility')=='major' or e.get('manual_fixed')]
    ranked = sorted(major, key=lambda e: (-e['salience'], e['start_at']))
    # Eight is a first-screen target. Never hide confirmed/high-value events
    # simply because an unusually rich day exceeds that target.
    high = [e for e in ranked if e.get('importance') == 'HIGH' or e.get('manual_fixed')]
    selected = high + [e for e in ranked if e not in high][:max(0, 8-len(high))]
    key_events = [{'text': e['title'], 'summary':e.get('summary',''),
                   'event_ids': [e['event_id']],
                   'utterance_ids': [r['utterance_id'] for r in e['evidence_snapshots']],
                   'start_at': e['start_at'], 'end_at': e['end_at']}
                  for e in selected]
    # Day prose consumes the grounded final events and authoritative task state.
    zone = ZoneInfo(sources[0]['timezone'] if sources else 'Asia/Singapore')
    def on_day(timestamp):
        return bool(timestamp and datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    .astimezone(zone).date().isoformat() == day)
    created = [t for t in task_items if on_day(t['created_at'])]
    completed = [t for t in task_items if t['status'] == 'completed' and on_day(t['completed_at'])]
    pending = [t for t in task_items if t['status'] == 'active']
    overview_claims = [{'text':e.get('summary',''), 'event_ids':[e['event_id']]}
                       for e in selected
                       if e.get('summary')]
    headline = ('今天主要围绕'+ '、'.join(e['text'] for e in key_events[:3])+'展开。'
                if key_events else '今天没有提取到值得总结的主要事件。')
    overview = ' '.join(c['text'] for c in overview_claims) or '尚无主要事件可供概述，零散录音内容保留在证据中。'
    return {'date': day, 'headline': headline, 'overview':overview,
            'overview_claims':overview_claims,
            'major_events':key_events,
            'headline_event_ids': [eid for e in key_events[:3] for eid in e['event_ids']],
            'key_events': key_events, 'tasks_created': created,
            'tasks_completed': completed, 'tasks_pending': pending,
            'people_interacted': list(people.values()),
            'unresolved_items': [{'text': t['title'], 'event_ids': t['event_ids'],
                                  'task_id': t['task_id']} for t in task_items if t['status'] == 'active'],
            'source_event_ids': [e['event_id'] for e in sources],
            'source_event_revisions': {e['event_id']: e['revision'] for e in sources},
            'events': sources, 'generation_version': SEMANTIC_VERSION,
            'statistics': {'event_count': len(sources), 'decision_count': 0,
                           'new_todo_count': len(created), 'unresolved_count': len(pending)},
            'decisions': [], 'new_todos': [{'event_id': t['task_id'], **t} for t in created],
            'unresolved': [{'event_id': t['task_id'], **t} for t in pending]}


def persist_summary(uow, events, tasks, day: str, timezone: str, start, end, now, *, semantic_error=None):
    objective = structured_summary(events, tasks, day)
    objective['semantic_status']='pending' if semantic_error else 'complete'
    objective['semantic_error']=semantic_error
    digest = canonical_json_sha256(objective)
    sid = stable_ulid('daily-summary', day, timezone)
    try:
        previous = uow.insights.daily(sid)
    except KeyError:
        previous = None
    if previous and previous['input_sha256'] == digest and previous['derivation_status'] == 'active':
        return previous
    generation = local_generation(uow, KnowledgeLayer.MEMORY, digest,
                                  {'day': day, 'source_event_ids': objective['source_event_ids'],
                                   'provider': 'local', 'remote_input_characters': 0}, now)
    revision = uow.insights.next_daily_revision(sid)
    # Retain V3.6 narrative compatibility; objective carries the structured V1.
    narrative = {'what_happened': [{'text': t['text'], 'evidence_event_ids': t['event_ids'],
                                    'evidence_utterance_ids': t['utterance_ids']} for t in objective['key_events']], 'decisions': [],
                 'new_todos': [{'text': t['title'], 'evidence_event_ids': t['event_ids'], 'evidence_utterance_ids': []}
                               for t in objective['tasks_created']],
                 'completed': [{'text': t['title'], 'evidence_event_ids': t['event_ids'], 'evidence_utterance_ids': []}
                               for t in objective['tasks_completed']],
                 'unresolved': [{'text': t['text'], 'evidence_event_ids': t['event_ids'], 'evidence_utterance_ids': []}
                                for t in objective['unresolved_items']],
                 'important_people_interactions': [], 'memorable_quotes': [], 'tomorrow_attention': []}
    uow.insights.add_daily(summary_id=sid, revision=revision, summary_date=day,
                          timezone=timezone, period_start=start.isoformat(), period_end=end.isoformat(),
                          objective=objective, narrative=narrative, input_sha256=digest,
                          generation_id=generation.generation_id,
                          provenance={'provider': 'local-event-derived', 'model': 'structured-event-synthesis',
                                      'generation_version': SEMANTIC_VERSION, 'remote_input_characters': 0,
                                      'semantic_analyzers':list({s.payload.get('semantic_provenance',{}).get('input_sha256','local'):
                                          s.payload.get('semantic_provenance',{}) for s in events}.values()),
                                      'semantic_reconcilers':list({p.get('input_sha256','local'):p for s in events
                                          for p in s.payload.get('semantic_reconciliation_provenance',[])}.values())},
                          status='active', created_by='local:daily', created_at=now.isoformat())
    ids = {s.event_id for s in events}
    task_ids = {t['event_id'] for t in tasks if any(t['event_id'] in s.payload['linked_task_ids'] for s in events)}
    utterances = {r['utterance_id'] for s in events for r in s.payload['evidence_snapshots']}
    _link_evidence(uow, 'daily_summary', sid, revision, ids | task_ids, utterances, now)
    uow.insights.add_operation(new_ulid(), 'daily_summary', sid, revision, 'generate',
                               'local:daily', {'input_sha256': digest}, now.isoformat())
    if previous:
        uow.derivations.complete_recompute_for_target('daily_summary', sid, previous['revision'],
                                                     generation.generation_id, now.isoformat())
    value = uow.insights.daily(sid)
    resource = {'summary_id': sid, 'revision': revision, 'summary_date': day,
                'timezone': timezone, 'status': value['status'],
                'derivation_status': value['derivation_status'], 'created_at': value['created_at'],
                'objective': {k: v for k, v in objective.items() if k != 'events'},
                'provenance': value['provenance']}
    uow.changes.append('daily_summary', sid, revision, 'upsert', resource)
    return value
