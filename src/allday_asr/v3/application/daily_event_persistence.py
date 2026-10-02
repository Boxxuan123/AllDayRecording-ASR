"""Daily reconciliation uses the existing event operation and dependency store."""
from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    DerivationDependency, EventCurrentState, EventKind, EventOperation,
    EventOperationKind, EventStatus, GenerationRecord, GenerationStatus,
    KnowledgeLayer, ProposalKind, ProposalStatus, StructuredProposal,
)
from .knowledge_support import cascade_derivations


def local_generation(uow, layer: KnowledgeLayer, digest: str, scope: dict, now):
    producer = 'daily-local'
    version = 'daily-local-v1'
    number = uow.knowledge.next_generation_number(layer.value, producer, version,
                                                'local-rules', version, version, digest)
    generation = GenerationRecord(new_ulid(), layer, producer, version, 'local-rules',
                                  version, version, scope, digest, number,
                                  GenerationStatus.SUCCEEDED, now, now)
    uow.knowledge.add_generation(generation)
    return generation


def persist_event(uow, identifier: str, payload: dict, current, now, *, retire=False):
    revision = current.revision + 1 if current else 1
    operation = (EventOperationKind.CANCEL if retire else
                 EventOperationKind.REOPEN if current and current.status == EventStatus.CANCELLED else
                 EventOperationKind.UPDATE if current else EventOperationKind.CREATE)
    session_id = current.session_id if current else payload['evidence_snapshots'][0]['session_id']
    evidence = payload['evidence_snapshots']
    digest = canonical_json_sha256(payload)
    generation = local_generation(uow, KnowledgeLayer.EVENT, digest,
                                  {'event_id': identifier, 'source_count': len(evidence),
                                   'input_characters': sum(len(r['text']) for r in evidence),
                                   'provider': 'local', 'remote_input_characters': 0}, now)
    proposal_id = new_ulid()
    patch = {'operation': operation.value, 'session_id': session_id,
             'event_kind': EventKind.IMPORTANT_EXPERIENCE.value,
             'expected_revision': revision - 1, 'event_id': identifier, 'patch': payload}
    uow.knowledge.add_proposal(StructuredProposal(
        proposal_id, generation.generation_id, ProposalKind.EVENT_OPERATION, patch,
        tuple(r['utterance_id'] for r in evidence), ProposalStatus.ACCEPTED, now,
        now, 'local:daily', 'deterministic_daily_reconciliation'))
    op = EventOperation(new_ulid(), identifier, session_id, EventKind.IMPORTANT_EXPERIENCE,
                        operation, revision, payload, 'local:daily', generation.generation_id,
                        proposal_id, now)
    uow.knowledge.add_event_operation(op)
    state = EventCurrentState(identifier, session_id, EventKind.IMPORTANT_EXPERIENCE,
                              EventStatus.CANCELLED if retire else EventStatus.ACTIVE,
                              revision, payload, op.operation_id,
                              current.created_at if current else now, now)
    uow.knowledge.put_event_state(state, revision - 1)
    if current:
        cascade_derivations(uow, source_type='event', source_id=identifier,
                            source_revision=current.revision,
                            reason='daily_event_reconciled', now=now)
        uow.derivations.complete_recompute_for_target('event', identifier, current.revision,
                                                     generation.generation_id, now.isoformat())
    for row in evidence:
        uid = row['utterance_id']
        uow.knowledge.add_evidence_link(new_ulid(), 'event', identifier, revision,
                                       'utterance', uid, row['revision'], now.isoformat())
        uow.derivations.add_dependency(DerivationDependency(
            'event', identifier, revision, 'utterance', uid, row['revision'], now))
    uow.changes.append('daily_event', identifier, revision,
                       'tombstone' if retire else 'upsert',
                       None if retire else event_resource(state))
    return state


def event_resource(state) -> dict:
    return {'event_id': state.event_id, 'revision': state.revision,
            'status': state.status.value, 'derivation_status': state.derivation_status,
            'created_at': state.created_at.isoformat(), 'updated_at': state.updated_at.isoformat(),
            **state.payload}
