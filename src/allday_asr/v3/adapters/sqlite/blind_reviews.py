"""Independent append-only truth; phone payload deliberately excludes predictions."""

import json
import re
from uuid import uuid4

from .dataset_reservations import now

PURITIES = {'clean_single', 'mixed_overlap', 'boundary_cross', 'uncertain'}


def latest_truth(connection):
    return {row['task_id']: dict(row) for row in connection.execute("""SELECT g.* FROM blind_ground_truth g
        WHERE NOT EXISTS(SELECT 1 FROM blind_ground_truth n WHERE n.task_id=g.task_id AND n.revision>g.revision)""")}


def phone_tasks(connection, *, history=False, task_id=None):
    truth = latest_truth(connection)
    tasks = connection.execute("""SELECT t.*,q.query_json,e.session_id FROM blind_review_tasks t
        JOIN blind_events e USING(event_id) JOIN blind_query_views q USING(query_id)
        WHERE e.superseded_by IS NULL AND (? IS NULL OR t.task_id=?)
        ORDER BY t.priority DESC,t.created_at,t.task_id""", (task_id, task_id)).fetchall()
    items = []
    for task in tasks:
        review = truth.get(task['task_id'])
        reviewed = review is not None and review['action'] == 'submit'
        if history != reviewed:
            continue
        query = json.loads(task['query_json'])
        candidate = {'prototype_id': task['task_id'], 'session_id': task['session_id'],
                     'speaker_track_id': query['speaker_track_id'], 'person_name': '待听原音',
                     'representative_clips': [{k: w[k] for k in ('media_id', 'start_ms', 'end_ms')} for w in query['windows']],
                     'review_status': 'reviewed' if reviewed else 'pending'}
        context = {'voice_mode': 'blind_identity_review', 'review_lane': 'history' if reviewed else 'primary',
                   'voice_candidates': [candidate], 'prototype_ids': [task['task_id']],
                   'duration_ms': round(query['duration_s'] * 1000)}
        if reviewed:
            context['purity_review'] = {'primary_speaker_person_id': review['primary_person_id'],
                'primary_speaker_unknown': review['unknown_kind'] != 'none', 'unknown_kind': review['unknown_kind'],
                'purity': review['purity'], 'other_speaker_ids': [], 'quality_flags': [], 'revision': review['revision']}
        items.append({'review_id': 'blind:' + task['task_id'], 'kind': 'blind_identity_review',
            'priority': 'normal', 'source_id': task['task_id'], 'source_revision': review['revision'] if review else None,
            'session_id': task['session_id'], 'person_id': None, 'title': 'Blind 人物验收',
            'summary': '听完整音频，确认主要人物与纯度', 'reason': 'blind_identity_review',
            'evidence_count': query['window_count'], 'created_at': task['created_at'],
            'updated_at': review['reviewed_at'] if review else task['created_at'], 'context': context})
    return items


def submit_truth(connection, task_id, payload, source):
    if set(payload) - {'review_id', 'action', 'operation_id', 'primary_speaker_person_id',
                       'primary_speaker_unknown', 'unknown_kind', 'purity', 'other_speaker_ids', 'quality_flags'}:
        raise ValueError('blind truth fields are invalid')
    if payload.get('operation_id') is not None and (not isinstance(payload['operation_id'], str)
        or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', payload['operation_id'])):
        raise ValueError('blind operation id is invalid')
    if payload.get('other_speaker_ids') or payload.get('quality_flags'):
        raise ValueError('blind truth only accepts primary person and query purity')
    action = payload.get('action')
    person = payload.get('primary_speaker_person_id')
    kind = payload.get('unknown_kind', 'dont_know' if payload.get('primary_speaker_unknown') is True else 'none')
    purity = payload.get('purity')
    if action == 'undo':
        person, kind, purity = None, 'dont_know', None
    elif action != 'submit' or purity not in PURITIES or kind not in {'none', 'stranger', 'dont_know', 'inaudible'}:
        raise ValueError('choose primary speaker and query purity')
    if action == 'submit' and ((kind == 'none' and not isinstance(person, str)) or (kind != 'none' and person is not None)):
        raise ValueError('choose a library person or an unknown category')
    if action == 'submit' and 'primary_speaker_unknown' in payload and (
        type(payload['primary_speaker_unknown']) is not bool or payload['primary_speaker_unknown'] != (kind != 'none')
    ):
        raise ValueError('unknown category contradicts primary speaker selection')
    if person is not None and connection.execute('SELECT 1 FROM persons WHERE person_id=? AND kind!=\'unknown\'', (person,)).fetchone() is None:
        raise ValueError('primary speaker does not exist')
    review_id = payload.get('operation_id') or uuid4().hex
    existing = connection.execute('SELECT * FROM blind_ground_truth WHERE review_id=?', (review_id,)).fetchone()
    if existing:
        if (existing['task_id'], existing['action'], existing['primary_person_id'], existing['unknown_kind'], existing['purity'], existing['review_source']) != (task_id, action, person, kind, purity, source):
            raise ValueError('operation_id reused with different blind truth')
        return dict(existing)
    task = connection.execute('SELECT 1 FROM blind_review_tasks t JOIN blind_events e USING(event_id) WHERE t.task_id=? AND e.superseded_by IS NULL', (task_id,)).fetchone()
    if task is None:
        raise ValueError('blind review task is missing or superseded; refresh inbox')
    latest = latest_truth(connection).get(task_id)
    if action == 'undo' and (latest is None or latest['action'] != 'submit'):
        raise ValueError('only submitted truth can be undone')
    revision = latest['revision'] + 1 if latest else 1
    connection.execute('INSERT INTO blind_ground_truth VALUES(?,?,?,?,?,?,?,?,?)',
                       (review_id, task_id, revision, action, person, kind, purity, source, now()))
    return dict(connection.execute('SELECT * FROM blind_ground_truth WHERE review_id=?', (review_id,)).fetchone())
