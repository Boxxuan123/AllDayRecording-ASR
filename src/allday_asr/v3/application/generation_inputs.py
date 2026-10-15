"""Versioned, actual model inputs and transaction-local publication checks."""
from dataclasses import asdict
from datetime import datetime

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.sound_kind import is_usable_speech
from .evidence_identity import project_evidence_identity


class GenerationSourceConflict(ValueError):
    """The evidence/context read by the producer no longer matches."""


def session_inputs(uow, session_id, kind):
    detail = uow.desktop.session_detail(session_id)
    references = {r['speaker_track_id']: r for r in detail['speaker_tracks']}
    utterances = []
    for row in detail['utterances']:
        if row['status'] != 'active' or not str(row['text']).strip() or not is_usable_speech(row.get('evidence', {})):
            continue
        reference = references.get(row.get('speaker_track_id'), {})
        value = project_evidence_identity(row, reference)
        if kind == 'reminder':
            value = {k: v for k, v in value.items() if k not in {'speaker_person_id', 'speaker_name'}}
            value['speaker_reference_id'] = (
                project_evidence_identity(row, reference)['speaker_person_id']
                or reference.get('speaker_cluster_id') or row.get('speaker_track_id')
            ) if 'person' not in row.get('evidence', {}).get('annotation_review', {}).get('dimensions', {}) else None
        utterances.append(value)
    result = {'session_id': session_id, 'captured_timezone': str(detail['session'].get('timezone') or 'UTC'),
              'utterances': utterances}
    if kind == 'reminder':
        fields = ('event_id', 'event_revision', 'title', 'actor_person_id', 'commitment_direction',
                  'related_person_ids', 'scheduled_at', 'location', 'status')
        result['active_reminders'] = [
            {k: row[k] for k in fields}
            for status in ('scheduled', 'delivered')
            for row in uow.reminders.list_schedules(status=status, session_id=session_id, due_before=None, limit=100)
        ]
    return result


def input_snapshot(kind, request):
    data = asdict(request)
    data.pop('now_utc', None)  # Fixed execution time, not a mutable source.
    data.pop('execution_id', None)
    return {'version': 1, 'kind': kind, 'inputs': data}


def product_inputs(uow, session_id, utterance_id, *, detail=None):
    """Only facts used by the one-source rule and its stable source identity."""
    detail = detail or uow.desktop.session_detail(session_id)
    row = next((r for r in detail['utterances'] if r['utterance_id'] == utterance_id), None)
    if row is None:
        raise GenerationSourceConflict('product reminder source is unavailable; re-extract')
    references = {r['speaker_track_id']: r for r in detail['speaker_tracks']}
    projected = project_evidence_identity(row, references.get(row.get('speaker_track_id')))
    return {'session_id': session_id, 'captured_timezone': str(detail['session'].get('timezone') or 'UTC'),
            'session_available': True, 'utterance': {
                **{k: projected[k] for k in ('utterance_id', 'revision', 'text', 'start_at', 'end_at', 'identity')},
                'status': row['status'], 'speech_usable': is_usable_speech(row.get('evidence', {}))}}


def product_snapshot(inputs):
    return {'version': 2, 'kind': 'product-reminder', 'inputs': inputs}


def _batch_created_reminder(uow, row, generation_id):
    """Prove an extra context row is exactly an unmodified accepted batch output."""
    if row['event_revision'] != 1 or row['status'] != 'scheduled':
        return False
    history = uow.knowledge.event_history(row['event_id'])
    if len(history) != 1:
        return False
    operation = history[0]
    if operation['generation_id'] != generation_id or operation['operation_kind'] != 'create':
        return False
    state = uow.knowledge.get_event(row['event_id'])
    if (state is None or state.derivation_status != 'active' or state.revision != 1
            or state.latest_operation_id != operation['operation_id'] or state.payload != operation['payload']):
        return False
    patch = operation['payload']
    for field in ('title', 'actor_person_id', 'commitment_direction', 'related_person_ids', 'location'):
        if row[field] != patch.get(field, [] if field == 'related_person_ids' else None):
            return False
    if datetime.fromisoformat(row['scheduled_at'].replace('Z', '+00:00')) != datetime.fromisoformat(patch['scheduled_time'].replace('Z', '+00:00')):
        return False
    candidate = uow.reminders.get_candidate_by_proposal(operation['proposal_id'])
    return bool(candidate and candidate.generation_id == generation_id
                and candidate.status.value in {'confirmed', 'auto_applied'}
                and candidate.matched_event_id == row['event_id']
                and uow.knowledge.get_proposal(candidate.proposal_id).status.value == 'accepted')


def validate_snapshot(uow, snapshot, *, generation_id=None):
    if not isinstance(snapshot, dict) or snapshot.get('version') not in {1, 2}:
        raise GenerationSourceConflict('generation requires a trusted input snapshot; regenerate or review sources')
    kind = snapshot.get('kind')
    data = snapshot.get('inputs', {})
    if snapshot['version'] == 2 and kind == 'product-reminder':
        try:
            current = product_inputs(uow, data['session_id'], data['utterance']['utterance_id'])
        except KeyError as exc:
            raise GenerationSourceConflict('product reminder source is unavailable; re-extract') from exc
    elif snapshot['version'] == 1 and kind in {'semantic-event', 'reminder'}:
        try:
            current = session_inputs(uow, data['session_id'], kind)
        except KeyError as exc:
            raise GenerationSourceConflict('generation source is unavailable; regenerate') from exc
        if kind == 'reminder' and generation_id is not None:
            original_ids = {r['event_id'] for r in data['active_reminders']}
            active_ids = {r['event_id'] for r in current['active_reminders']}
            if set(uow.reminders.generation_created_outputs(generation_id)) - active_ids:
                raise GenerationSourceConflict('generation source conflict: accepted batch output is no longer active; regenerate')
            current['active_reminders'] = [r for r in current['active_reminders']
                if r['event_id'] in original_ids or not _batch_created_reminder(uow, r, generation_id)]
    elif snapshot['version'] == 1 and kind == 'command':
        current = {'utterances': [], 'events': []}
        ids = tuple(r['utterance_id'] for r in data['utterances'])
        facts = uow.knowledge.utterance_revisions(ids)
        current['utterances'] = [
            {'utterance_id': i, 'session_id': facts[i][0], 'revision': facts[i][1]}
            for i in ids if i in facts
        ]
        for row in data['events']:
            event = uow.knowledge.get_event(row['event_id'])
            if event is not None and event.derivation_status == 'active':
                current['events'].append({'event_id': event.event_id, 'revision': event.revision})
    else:
        raise GenerationSourceConflict('unsupported generation input snapshot; regenerate')
    if canonical_json_sha256(current) != canonical_json_sha256(data):
        raise GenerationSourceConflict('generation source conflict: evidence or context changed; regenerate')
