from __future__ import annotations

from datetime import datetime
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.knowledge import GenerationSubmission, KnowledgeLayer, ProposalKind
from allday_asr.v3.domain.people import PersonKind
from allday_asr.v3.ports.repositories import UnitOfWork

from .durable_processing import CorrectUtteranceCommand, apply_utterance_correction
from .people_support import (
    _CLUSTER_IDENTITY_ACTOR_PREFIX,
    _datetime,
    _identity_for_person_kind,
    _replace_reference,
)


class PeopleClusterMixin:
    def cluster(self, cluster_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.people.cluster_detail(cluster_id)
    def create_person(self, display_name: str, kind: PersonKind = PersonKind.KNOWN) -> dict[str, Any]:
        name = display_name.strip()
        if not name:
            raise ValueError("person display name is required")
        if kind is PersonKind.UNKNOWN:
            raise ValueError("use an anonymous cluster instead of an unknown person")
        person_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.create_person(person_id, name, kind.value, _datetime(self._now()))
        return {"person_id": person_id, "display_name": name, "kind": kind.value}
    def label_cluster(
        self, cluster_id: str, person_id: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        return self._link_cluster(
            cluster_id,
            person_id,
            actor=actor,
            source="human",
            confidence=1.0,
            promote_candidates=False,
        )
    def _link_cluster(
        self,
        cluster_id: str,
        person_id: str,
        *,
        actor: str,
        source: str,
        confidence: float,
        promote_candidates: bool,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("speaker label actor is required")
        operation_id = new_ulid()
        now = self._now()
        with self._uow_factory() as uow:
            person_kind = uow.people.person_kind(person_id)
            previous_person_id, _, event_ids = uow.people.label_cluster(
                cluster_id,
                person_id,
                actor,
                operation_id,
                _datetime(now),
                source=source,
                confidence=confidence,
                promote_candidates=promote_candidates,
            )
            identity_changes = self._sync_cluster_identity(
                uow,
                cluster_id,
                _identity_for_person_kind(person_kind),
                actor,
                now,
            )
            events = uow.people.events_by_ids(event_ids)
        rebound: list[str] = []
        warnings: list[str] = []
        for event in events:
            with self._uow_factory() as uow:
                evidence_ids = uow.people.cluster_evidence_ids(
                    cluster_id, str(event["session_id"])
                )
            if not evidence_ids:
                warnings.append(f"event {event['event_id']} has no same-session evidence")
                continue
            try:
                self._rebind_event(
                    event,
                    previous_person_id or cluster_id,
                    person_id,
                    evidence_ids,
                    actor,
                )
                rebound.append(str(event["event_id"]))
            except ValueError as exc:
                warnings.append(f"event {event['event_id']}: {exc}")
        with self._uow_factory() as uow:
            migrated_memories = uow.person_memories.reconcile_identity(
                cluster_id,
                previous_person_id,
                person_id,
                actor,
                _datetime(self._now()),
            )
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "person_id": person_id,
            "previous_person_id": previous_person_id,
            "link_source": source,
            "link_confidence": confidence,
            "rebound_event_ids": rebound,
            "warnings": warnings,
            "migrated_memory_count": migrated_memories,
            **identity_changes,
        }
    def create_and_label(
        self, cluster_id: str, display_name: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        person = self.create_person(display_name)
        return {**self.label_cluster(cluster_id, person["person_id"], actor), "person": person}
    def merge(
        self,
        source_cluster_ids: tuple[str, ...],
        target_cluster_id: str,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.merge_clusters(
                source_cluster_ids,
                target_cluster_id,
                actor,
                operation_id,
                _datetime(self._now()),
            )
        return {"operation_id": operation_id, "target_cluster_id": target_cluster_id}
    def split(
        self,
        cluster_id: str,
        speaker_track_ids: tuple[str, ...],
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        new_cluster_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.split_cluster(
                cluster_id,
                speaker_track_ids,
                new_cluster_id,
                "拆分后的未知说话人",
                actor,
                operation_id,
                _datetime(self._now()),
            )
        return {
            "operation_id": operation_id,
            "source_cluster_id": cluster_id,
            "new_cluster_id": new_cluster_id,
        }
    def ignore(
        self, cluster_id: str, reason: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.ignore_cluster(
                cluster_id, reason, actor, operation_id, _datetime(self._now())
            )
        return {"operation_id": operation_id, "cluster_id": cluster_id, "status": "ignored"}
    def undo(self, cluster_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        operation_id = new_ulid()
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.people.cluster_detail(cluster_id)
            reverted = uow.people.undo(
                cluster_id, actor, operation_id, _datetime(now)
            )
            if reverted["kind"] == "label":
                previous_person_id = reverted["payload"].get("previous_person_id")
                restored_identity = (
                    _identity_for_person_kind(uow.people.person_kind(previous_person_id))
                    if previous_person_id
                    else SelfIdentity.UNKNOWN
                )
                identity_changes = self._sync_cluster_identity(
                    uow,
                    cluster_id,
                    restored_identity,
                    actor,
                    now,
                )
            else:
                identity_changes = {
                    "updated_utterance_count": 0,
                    "preserved_manual_utterance_count": 0,
                }
            event_ids = tuple(reverted["payload"].get("rebound_event_ids", ()))
            events = uow.people.events_by_ids(event_ids)
        rebound: list[str] = []
        warnings: list[str] = []
        if reverted["kind"] == "label" and current.get("person_id"):
            replacement = reverted["payload"].get("previous_person_id") or cluster_id
            for event in events:
                with self._uow_factory() as uow:
                    evidence_ids = uow.people.cluster_evidence_ids(
                        cluster_id, str(event["session_id"])
                    )
                if not evidence_ids:
                    warnings.append(f"event {event['event_id']} has no same-session evidence")
                    continue
                try:
                    self._rebind_event(
                        event,
                        str(current["person_id"]),
                        str(replacement),
                        evidence_ids,
                        actor,
                    )
                    rebound.append(str(event["event_id"]))
                except ValueError as exc:
                    warnings.append(f"event {event['event_id']}: {exc}")
            with self._uow_factory() as uow:
                migrated_memories = uow.person_memories.reconcile_identity(
                    cluster_id,
                    str(current["person_id"]),
                    (
                        str(reverted["payload"]["previous_person_id"])
                        if reverted["payload"].get("previous_person_id")
                        else None
                    ),
                    actor,
                    _datetime(self._now()),
                )
        else:
            migrated_memories = 0
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "reverted_operation_id": reverted["operation_id"],
            "rebound_event_ids": rebound,
            "warnings": warnings,
            "migrated_memory_count": migrated_memories,
            **identity_changes,
        }
    @staticmethod
    def _sync_cluster_identity(
        uow: UnitOfWork,
        cluster_id: str,
        identity: SelfIdentity,
        actor: str,
        now: datetime,
    ) -> dict[str, int]:
        updated = 0
        preserved = 0
        for utterance_id in uow.people.cluster_evidence_ids(cluster_id):
            utterance = uow.evidence.get_utterance(utterance_id)
            if utterance.identity is identity:
                continue
            identity_corrections = tuple(
                correction
                for correction in uow.corrections.list_for_target(
                    "utterance", utterance_id
                )
                if "identity" in correction.patch
            )
            if identity_corrections:
                if not identity_corrections[-1].actor.startswith(
                    _CLUSTER_IDENTITY_ACTOR_PREFIX
                ):
                    preserved += 1
                    continue
            elif utterance.identity is not SelfIdentity.UNKNOWN:
                preserved += 1
                continue
            apply_utterance_correction(
                uow,
                CorrectUtteranceCommand(
                    utterance_id=utterance.utterance_id,
                    expected_revision=utterance.revision,
                    text=utterance.text,
                    actor=f"{_CLUSTER_IDENTITY_ACTOR_PREFIX}{actor}",
                    identity=identity,
                    change_identity=True,
                ),
                now,
            )
            updated += 1
        return {
            "updated_utterance_count": updated,
            "preserved_manual_utterance_count": preserved,
        }
    def _rebind_event(
        self,
        event: dict[str, Any],
        old_reference: str,
        person_id: str,
        evidence_ids: tuple[str, ...],
        actor: str,
    ) -> None:
        next_payload = _replace_reference(event["payload"], old_reference, person_id)
        if next_payload == event["payload"]:
            return
        submission = GenerationSubmission(
            layer=KnowledgeLayer.EVENT,
            producer="human-identity-correction",
            producer_version="3.4.0",
            model="none",
            prompt_version="person-reference-rebind-v1",
            extractor_version="person-reference-rebind-v1",
            input_scope={
                "source": "person_cluster_label",
                "cluster_id": old_reference,
                "person_id": person_id,
                "event_id": event["event_id"],
            },
            proposals=(
                (
                    ProposalKind.EVENT_OPERATION,
                    {
                        "operation": "update",
                        "event_id": event["event_id"],
                        "session_id": event["session_id"],
                        "event_kind": event["event_kind"],
                        "expected_revision": int(event["revision"]),
                        "patch": next_payload,
                    },
                    evidence_ids,
                ),
            ),
        )
        generation = self._knowledge.submit_generation(submission)
        proposal_id = str(generation["proposals"][0]["proposal_id"])
        self._knowledge.accept_proposal(proposal_id, actor)
