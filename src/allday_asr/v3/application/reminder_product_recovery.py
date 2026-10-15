"""Read-only eligibility for explicit recovery of legacy rule candidates."""
from allday_asr.v3.domain.hashing import canonical_json_sha256
from .generation_inputs import product_inputs, GenerationSourceConflict


def recovery_report(uow, candidate_id):
    candidate = uow.reminders.get_candidate(candidate_id)
    proposal = uow.knowledge.get_proposal(candidate.proposal_id)
    generation = uow.knowledge.get_generation(candidate.generation_id)
    scope = generation.input_scope
    key = scope.get('product_source_key')
    report = {'candidate_id': candidate_id, 'generation_id': generation.generation_id,
              'status': candidate.status.value, 'conflict_reason': candidate.conflict_reason,
              'source_versions': scope.get('resolved_inputs', {}).get('utterances', []),
              'eligible': False, 'reason': 'requires_source_review', 'replacement_candidate_id': None}
    if generation.producer != 'product-reminder' or not isinstance(key, str):
        return report
    redirected = uow.idempotency.response(key + ':recovery')
    if redirected:
        report.update(reason='already_recovered', replacement_candidate_id=redirected['candidate_id'])
        return report
    if candidate.status.value not in {'conflict', 'pending_confirmation'}:
        report['reason'] = 'user_decision_preserved'
        return report
    snapshot = scope.get('input_snapshot', {})
    if snapshot.get('version') != 1 or snapshot.get('kind') != 'reminder':
        return report
    if candidate.status.value == 'conflict' and candidate.conflict_reason != 'generation source conflict: evidence or context changed; regenerate':
        return report
    data = snapshot.get('inputs', {})
    rows = [r for r in data.get('utterances', []) if r.get('utterance_id') in proposal.evidence_utterance_ids]
    if len(rows) != 1 or len(proposal.evidence_utterance_ids) != 1:
        return report
    row = rows[0]
    required = ('utterance_id', 'revision', 'text', 'start_at', 'end_at', 'identity')
    if any(k not in row for k in required) or any(k not in scope for k in ('source_text', 'recorded_at', 'timezone', 'utterance_revision')):
        return report
    if (row['identity'] != 'self' or scope['source_text'] != row['text'] or scope['recorded_at'] != row['start_at']
            or scope['utterance_revision'] != row['revision'] or scope['timezone'] != data.get('captured_timezone')):
        return report
    historical = {'session_id': data.get('session_id'), 'captured_timezone': data.get('captured_timezone'),
                  'session_available': True, 'utterance': {**{k: row[k] for k in required},
                                                        'status': 'active', 'speech_usable': True}}
    try:
        current = product_inputs(uow, candidate.session_id, row['utterance_id'])
    except (KeyError, GenerationSourceConflict):
        report['reason'] = 'source_unavailable'
        return report
    if canonical_json_sha256(current) != canonical_json_sha256(historical):
        report['reason'] = 'actual_source_changed'
        return report
    report.update(eligible=True, reason='legacy_rule_context_conflict', product_source_key=key,
                  current_inputs=current)
    return report
