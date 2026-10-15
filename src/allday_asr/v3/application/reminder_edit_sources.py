"""Resolve manual reminder provenance from persisted candidate relationships."""
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import GenerationStatus, ProposalKind, ProposalStatus
from allday_asr.v3.domain.reminders import ReminderCandidateStatus, ReminderFeedback, ReminderFeedbackAction
from .generation_inputs import GenerationSourceConflict, validate_snapshot
from .reminder_support import _datetime


def edit_sources(uow, producer, model, scope, proposals, *, candidate_id=None):
    """Return the actual original batch and pending ancestors; never trust a batch ID."""
    ancestors = []
    seen = {candidate_id} if candidate_id else set()
    while 'revises_candidate_id' in scope:
        if producer not in {'desktop-user', 'phone-user'} or model != 'manual-edit':
            raise GenerationSourceConflict('reminder edit source must be a manual operation')
        allowed = {'session_id', 'revises_candidate_id', 'input_snapshot', 'resolved_inputs', 'product_source_key', 'operation_id'}
        if set(scope) - allowed or len(proposals) != 1:
            raise GenerationSourceConflict('reminder edit source relationship is invalid')
        parent_id = scope['revises_candidate_id']
        if not isinstance(parent_id, str) or not parent_id or parent_id in seen:
            raise GenerationSourceConflict('reminder edit source relationship is cyclic or missing')
        seen.add(parent_id)
        try:
            parent = uow.reminders.get_candidate(parent_id)
            proposal = uow.knowledge.get_proposal(parent.proposal_id)
            generation = uow.knowledge.get_generation(proposal.generation_id)
        except KeyError as exc:
            raise GenerationSourceConflict('reminder edit source relationship is missing') from exc
        snapshot = generation.input_scope.get('input_snapshot')
        data = snapshot.get('inputs', {}) if isinstance(snapshot, dict) else {}
        if snapshot and snapshot.get('kind') == 'command':
            rows = data.get('utterances', [])
            source_session_matches = all(row.get('session_id') == parent.session_id for row in rows)
        else:
            rows = [data.get('utterance', {})] if snapshot and snapshot.get('kind') == 'product-reminder' else data.get('utterances', [])
            source_session_matches = data.get('session_id') == parent.session_id
        if not source_session_matches or not set(proposal.evidence_utterance_ids).issubset({row.get('utterance_id') for row in rows}):
            raise GenerationSourceConflict('reminder edit snapshot does not contain its candidate source')
        kind, payload, evidence = proposals[0]
        if (parent.status is not ReminderCandidateStatus.PENDING_CONFIRMATION
                or proposal.status is not ProposalStatus.PENDING
                or generation.status is not GenerationStatus.SUCCEEDED):
            raise GenerationSourceConflict('original reminder candidate is no longer pending')
        if (parent.generation_id != proposal.generation_id or payload.get('patch', {}).get('needs_confirmation') is not True
                or kind is not ProposalKind.EVENT_OPERATION
                or proposal.kind is not ProposalKind.EVENT_OPERATION
                or scope.get('session_id') != parent.session_id
                or payload.get('session_id') != parent.session_id
                or proposal.payload.get('session_id') != parent.session_id
                or generation.input_scope.get('session_id') != parent.session_id
                or tuple(evidence) != proposal.evidence_utterance_ids
                or canonical_json_sha256(scope.get('input_snapshot')) != canonical_json_sha256(generation.input_scope.get('input_snapshot'))):
            raise GenerationSourceConflict('reminder edit source relationship does not match its candidate')
        def immutable(value):
            return {**{k: v for k, v in value.items() if k != 'patch'},
                    'patch': {k: v for k, v in value['patch'].items() if k not in {'title', 'scheduled_time', 'location', 'needs_confirmation'}}}
        if immutable(payload) != immutable(proposal.payload):
            raise GenerationSourceConflict('reminder edit cannot change source, operation or target revision')
        ancestors.append(parent)
        scope, producer, model = generation.input_scope, generation.producer, generation.model
        proposals = ((proposal.kind, proposal.payload, proposal.evidence_utterance_ids),)
    if producer == 'desktop-user' and model == 'manual-edit':
        raise GenerationSourceConflict('reminder edit requires its persisted source candidate')
    return (generation.generation_id if ancestors else None), tuple(ancestors)


def validate_generation_sources(uow, generation, proposal):
    candidate = uow.reminders.get_candidate_by_proposal(proposal.proposal_id)
    if ('revises_candidate_id' in generation.input_scope and candidate is None) or (candidate is not None and (
            candidate.generation_id != generation.generation_id or proposal.generation_id != generation.generation_id
            or candidate.session_id != proposal.payload.get('session_id')
            or candidate.status is not ReminderCandidateStatus.PENDING_CONFIRMATION)):
        raise GenerationSourceConflict('reminder edit candidate relationship is missing or inconsistent')
    source_id, ancestors = edit_sources(uow, generation.producer, generation.model,
        generation.input_scope, ((proposal.kind, proposal.payload, proposal.evidence_utterance_ids),),
        candidate_id=candidate.candidate_id if candidate else None)
    validate_snapshot(uow, generation.input_scope.get('input_snapshot'),
                      generation_id=source_id or generation.generation_id)
    return ancestors


def finalize_edit_sources(uow, ancestors, replacement_id, resource_id, actor, now):
    """Only called inside the acceptance transaction after validated publication."""
    for original in ancestors:
        uow.knowledge.resolve_proposal(original.proposal_id, 'rejected', _datetime(now), actor, 'superseded_by_user_edit')
        uow.reminders.resolve_candidate(original.candidate_id, 'modified', resource_id, None, _datetime(now), actor)
        if not uow.reminders.add_feedback(ReminderFeedback(new_ulid(), original.candidate_id,
                ReminderFeedbackAction.MODIFY, actor, {'replacement_candidate_id': replacement_id}, now)):
            raise RuntimeError('reminder edit feedback identity collision')
