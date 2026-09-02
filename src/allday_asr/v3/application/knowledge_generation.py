from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.hashing import canonical_json_sha256
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.knowledge import (
    EvidenceSpan,
    GenerationRecord,
    GenerationStatus,
    GenerationSubmission,
    ProposalKind,
    ProposalStatus,
    StructuredProposal,
)

from .knowledge_support import (
    ProposalAcceptedHook,
    ProposalCreatedHook,
    ProposalRejectedHook,
    ProposalResolution,
    _datetime,
    _validate_submission,
)


class KnowledgeGenerationMixin:
    def materialize_evidence(self, session_id: str) -> tuple[dict[str, Any], ...]:
        now = self._now()
        with self._uow_factory() as uow:
            uow.catalog.get_session(session_id)
            for value in uow.knowledge.unmaterialized_evidence(session_id):
                span = EvidenceSpan(
                    evidence_span_id=stable_ulid(
                        "evidence-span",
                        value["utterance_id"],
                        value["asset_id"],
                        value["session_start_ms"],
                        value["session_end_ms"],
                    ),
                    session_id=str(value["session_id"]),
                    asset_id=str(value["asset_id"]),
                    artifact_id=str(value["source_artifact_id"]),
                    utterance_id=str(value["utterance_id"]),
                    session_start_ms=int(value["session_start_ms"]),
                    session_end_ms=int(value["session_end_ms"]),
                    asset_start_ms=int(value["asset_start_ms"]),
                    asset_end_ms=int(value["asset_end_ms"]),
                    created_at=now,
                )
                uow.knowledge.add_evidence_span(span)
            return uow.knowledge.list_evidence(session_id)

    def submit_generation(
        self,
        submission: GenerationSubmission,
        *,
        on_proposal_created: ProposalCreatedHook | None = None,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        now = self._now()
        _validate_submission(submission, allow_empty=allow_empty)
        with self._uow_factory() as uow:
            normalized_scope = self._resolve_inputs(uow, submission)
            input_sha256 = canonical_json_sha256(normalized_scope)
            generation_number = uow.knowledge.next_generation_number(
                submission.layer.value,
                submission.producer,
                submission.producer_version,
                submission.model,
                submission.prompt_version,
                submission.extractor_version,
                input_sha256,
            )
            generation = GenerationRecord(
                generation_id=new_ulid(),
                layer=submission.layer,
                producer=submission.producer,
                producer_version=submission.producer_version,
                model=submission.model,
                prompt_version=submission.prompt_version,
                extractor_version=submission.extractor_version,
                input_scope=normalized_scope,
                input_sha256=input_sha256,
                generation_number=generation_number,
                status=GenerationStatus.COLLECTING,
                created_at=now,
            )
            if not uow.knowledge.add_generation(generation):
                raise RuntimeError("generation identity collision")
            proposals: list[StructuredProposal] = []
            for index, (kind, payload, evidence_ids) in enumerate(submission.proposals):
                proposal = StructuredProposal(
                    proposal_id=new_ulid(),
                    generation_id=generation.generation_id,
                    kind=kind,
                    payload=dict(payload),
                    evidence_utterance_ids=tuple(evidence_ids),
                    status=ProposalStatus.PENDING,
                    created_at=now,
                )
                if not uow.knowledge.add_proposal(proposal):
                    raise RuntimeError("proposal identity collision")
                proposals.append(proposal)
                if on_proposal_created is not None:
                    on_proposal_created(uow, generation, proposal, index, now)
            completed_at = _datetime(now)
            uow.knowledge.complete_generation(
                generation.generation_id,
                GenerationStatus.SUCCEEDED.value,
                completed_at,
                None,
            )
            uow.audit.append(
                "knowledge.generation.submitted",
                f"producer:{submission.producer}",
                "generation",
                generation.generation_id,
                {
                    "layer": submission.layer.value,
                    "input_sha256": input_sha256,
                    "generation_number": generation_number,
                    "proposal_count": len(proposals),
                },
            )
            return {
                "generation_id": generation.generation_id,
                "layer": generation.layer.value,
                "input_sha256": generation.input_sha256,
                "generation_number": generation.generation_number,
                "status": GenerationStatus.SUCCEEDED.value,
                "proposals": [
                    {
                        "proposal_id": proposal.proposal_id,
                        "kind": proposal.kind.value,
                        "status": proposal.status.value,
                    }
                    for proposal in proposals
                ],
            }

    def accept_proposal(
        self,
        proposal_id: str,
        actor: str,
        *,
        on_accepted: ProposalAcceptedHook | None = None,
    ) -> ProposalResolution:
        if not actor.strip():
            raise ValueError("proposal resolver is required")
        now = self._now()
        with self._uow_factory() as uow:
            proposal = uow.knowledge.get_proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise ValueError("proposal is no longer pending")
            generation = uow.knowledge.get_generation(proposal.generation_id)
            if generation.status is not GenerationStatus.SUCCEEDED:
                raise ValueError("proposal generation is not usable")
            if proposal.kind is ProposalKind.EVENT_OPERATION:
                resolution = self._accept_event(uow, proposal, actor, now)
            else:
                resolution = self._accept_memory(uow, proposal, actor, now)
            uow.knowledge.resolve_proposal(
                proposal_id,
                ProposalStatus.ACCEPTED.value,
                _datetime(now),
                actor,
                None,
            )
            if on_accepted is not None:
                on_accepted(uow, proposal, resolution, now)
            uow.audit.append(
                "knowledge.proposal.accepted",
                actor,
                "proposal",
                proposal_id,
                resolution.as_dict(),
            )
            return resolution

    def reject_proposal(
        self,
        proposal_id: str,
        actor: str,
        reason: str,
        *,
        on_rejected: ProposalRejectedHook | None = None,
    ) -> ProposalResolution:
        if not actor.strip() or not reason.strip():
            raise ValueError("proposal rejection requires actor and reason")
        now = self._now()
        with self._uow_factory() as uow:
            proposal = uow.knowledge.get_proposal(proposal_id)
            if proposal.status is not ProposalStatus.PENDING:
                raise ValueError("proposal is no longer pending")
            uow.knowledge.resolve_proposal(
                proposal_id,
                ProposalStatus.REJECTED.value,
                _datetime(now),
                actor,
                reason.strip(),
            )
            if on_rejected is not None:
                on_rejected(uow, proposal, now)
            uow.audit.append(
                "knowledge.proposal.rejected",
                actor,
                "proposal",
                proposal_id,
                {"reason": reason.strip()},
            )
        return ProposalResolution(
            proposal_id,
            ProposalStatus.REJECTED,
            None,
            None,
            None,
        )
