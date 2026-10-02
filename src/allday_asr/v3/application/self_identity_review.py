"""Human identity corrections without person/sample annotation learning."""
import json

from allday_asr.v3.domain.identity import SelfIdentity
from .durable_processing import CorrectUtteranceCommand, apply_utterance_correction
from .historical_self_backfill import query_snapshot


def list_reviews(factory, item_id=None):
    with factory().reading() as uow:
        rows = uow.self_backfill.review_rows(item_id)
        result = []
        for row in rows:
            trace = json.loads(row['trace_json'])
            completed = row['action'] is not None
            if not completed and (row['revision'] != row['source_revision'] or row['status'] != 'active'
                                  or row['identity'] != 'unknown' or uow.self_backfill.session_skip(row['session_id'])):
                continue
            clips = [{'media_id': w['media_id'], 'start_ms': w['start_ms'], 'end_ms': w['end_ms']}
                     for w in trace['inputs'].get('windows', [])]
            result.append({
                'review_id': 'self:' + row['item_id'], 'kind': 'self_identity_review',
                'priority': 'normal', 'source_id': row['item_id'], 'source_revision': row['source_revision'],
                'session_id': row['session_id'], 'person_id': None, 'title': '可能是本人',
                'summary': '本人匹配较高，但有效语音不足自动确认要求', 'reason': 'self_identity_review',
                'evidence_count': len(clips), 'created_at': row['created_at'], 'updated_at': row['created_at'],
                'context': {'voice_mode': 'self_identity_review', 'review_lane': 'history' if completed else 'primary',
                    'review_action': row['action'], 'source_text': row['text'], 'start_at': row['start_at'],
                    'duration_ms': trace['duration_ms'], 'profile_learning': False,
                    'prototype_ids': [row['item_id']], 'voice_candidates': [{
                        'prototype_id': row['item_id'], 'session_id': row['session_id'],
                        'speaker_track_id': trace['inputs'].get('speaker_track_id', ''),
                        'person_name': '可能是本人', 'review_status': row['action'] or 'pending',
                        'representative_clips': clips}]}})
        return result


def resolve_review(people, payload, actor):
    item_id = payload['review_id'].removeprefix('self:')
    action, operation = payload['action'], payload.get('operation_id')
    if action not in {'confirm', 'reject', 'uncertain'} or not operation:
        raise ValueError('identity review requires a durable operation and valid action')
    if payload.get('prototype_id') != item_id:
        raise ValueError('identity review sample mismatch')
    with people._uow_factory() as uow:
        prior = uow.self_backfill.prior_review(operation)
        if prior:
            if (prior['item_id'], prior['action'], prior['actor']) != (item_id, action, actor):
                raise ValueError('identity review operation reused for another decision')
            return json.loads(prior['result_json'])
        rows = uow.self_backfill.review_rows(item_id)
        if not rows:
            raise ValueError('identity review no longer exists')
        item = rows[0]
        if item['action']:
            # Another device already resolved the object: acknowledge, never overwrite.
            return json.loads(item['result_json']) | {'already_resolved': True}
        row = uow.evidence.get_utterance(item['utterance_id'])
        history = uow.corrections.list_for_target('utterance', row.utterance_id)
        if (row.revision != item['source_revision'] or row.status != 'active' or row.identity is not SelfIdentity.UNKNOWN
            or row.evidence.get('person_annotation') or uow.self_backfill.session_skip(row.session_id)
            or any('identity' in c.patch and not c.actor.startswith('system:') for c in history)):
            raise ValueError('identity source changed; refresh review inbox')
        trace = json.loads(item['trace_json'])
        query = next((q for q in uow.people.product_self_queries(row.session_id, people._artifact_root)
                      if q['utterance_id'] == row.utterance_id), None)
        if query is None or query_snapshot(query) != trace['inputs']:
            raise ValueError('identity audio mapping/ownership changed; refresh review inbox')
        now = people._now()
        if action != 'uncertain':
            apply_utterance_correction(uow, CorrectUtteranceCommand(
                row.utterance_id, row.revision, row.text, actor,
                identity=SelfIdentity.SELF if action == 'confirm' else SelfIdentity.NOT_SELF,
                change_identity=True), now)
        result = {'item_id': item_id, 'action': action,
                  'identity': uow.evidence.get_utterance(row.utterance_id).identity.value,
                  'source': 'human_confirmed_self' if action == 'confirm' else 'human_confirmed_non_self' if action == 'reject' else 'reviewed_uncertain',
                  'profile_learning': False}
        uow.self_backfill.record_review(item_id, operation, action, actor, result, now.isoformat())
        uow.audit.append('self_identity.reviewed', actor, 'utterance', row.utterance_id, result)
        return result
