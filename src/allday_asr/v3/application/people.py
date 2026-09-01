from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.domain.people import (
    ClusterMatchPolicy,
    PersonKind,
    conservative_match,
)
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.speaker_embeddings import SpeakerEmbeddingProvider

from allday_asr.v3.application.knowledge import KnowledgeArchitectureService


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class SpeakerIdentityService:
    """Open-set identity workflow; only explicit labels publish stable prototypes."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_provider: SpeakerEmbeddingProvider,
        knowledge: KnowledgeArchitectureService,
        *,
        policy: ClusterMatchPolicy | None = None,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = embedding_provider
        self._knowledge = knowledge
        self._policy = policy or ClusterMatchPolicy()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def analyze(self, session_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            tracks = uow.people.analysis_inputs(session_id)
            run_id = new_ulid()
            started_at = _datetime(self._now())
            uow.people.start_run(
                run_id,
                session_id,
                self._provider.model,
                self._provider.model_version,
                self._policy_dict(),
                len(tracks),
                started_at,
            )
        try:
            embeddings = self._provider.embed(tracks)
            embedded_ids = {value.speaker_track_id for value in embeddings}
            missing = sorted(
                track.speaker_track_id for track in tracks
                if track.speaker_track_id not in embedded_ids
            )
            created = 0
            matched = 0
            suggestions = 0
            with self._uow_factory() as uow:
                for index, embedding in enumerate(embeddings, start=1):
                    anonymous = _compatible(
                        embedding.vector,
                        uow.people.cluster_vectors(embedding.model, embedding.model_version),
                    )
                    anonymous_match = conservative_match(
                        embedding.vector,
                        anonymous,
                        threshold=self._policy.anonymous_threshold,
                        minimum_margin=self._policy.minimum_margin,
                    )
                    create_cluster = anonymous_match.target_id is None
                    cluster_id = anonymous_match.target_id or new_ulid()
                    if create_cluster:
                        created += 1
                    else:
                        matched += 1
                    people = _compatible(
                        embedding.vector,
                        uow.people.person_vectors(embedding.model, embedding.model_version),
                    )
                    suggestion = conservative_match(
                        embedding.vector,
                        people,
                        threshold=self._policy.person_suggestion_threshold,
                        minimum_margin=self._policy.minimum_margin,
                    )
                    if suggestion.target_id is not None:
                        suggestions += 1
                    uow.people.record_embedding(
                        run_id=run_id,
                        cluster_id=cluster_id,
                        cluster_label=f"未知说话人 {index:02d}",
                        create_cluster=create_cluster,
                        membership_id=new_ulid(),
                        prototype_id=new_ulid(),
                        operation_id=new_ulid(),
                        embedding=embedding,
                        membership_confidence=(anonymous_match.score or 1.0),
                        suggested_person_id=suggestion.target_id,
                        suggestion_confidence=suggestion.score if suggestion.target_id else None,
                        created_at=_datetime(self._now()),
                    )
                uow.people.finish_run(
                    run_id, "succeeded", _datetime(self._now()), None
                )
            return {
                "cluster_run_id": run_id,
                "session_id": session_id,
                "status": "succeeded",
                "track_count": len(tracks),
                "embedded_track_count": len(embeddings),
                "new_cluster_count": created,
                "matched_track_count": matched,
                "person_suggestion_count": suggestions,
                "unusable_track_ids": missing,
            }
        except Exception as exc:
            with self._uow_factory() as uow:
                uow.people.finish_run(
                    run_id, "failed", _datetime(self._now()), str(exc)[:2_000]
                )
            raise

    def list_people(self) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            return uow.people.list_people()

    def list_clusters(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None and status not in {"active", "merged", "split", "ignored"}:
            raise ValueError("speaker cluster status is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("speaker cluster limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.people.list_clusters(status, limit)

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
        if not actor.strip():
            raise ValueError("speaker label actor is required")
        operation_id = new_ulid()
        with self._uow_factory() as uow:
            previous_person_id, _, event_ids = uow.people.label_cluster(
                cluster_id, person_id, actor, operation_id, _datetime(self._now())
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
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "person_id": person_id,
            "previous_person_id": previous_person_id,
            "rebound_event_ids": rebound,
            "warnings": warnings,
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
        with self._uow_factory() as uow:
            current = uow.people.cluster_detail(cluster_id)
            reverted = uow.people.undo(
                cluster_id, actor, operation_id, _datetime(self._now())
            )
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
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "reverted_operation_id": reverted["operation_id"],
            "rebound_event_ids": rebound,
            "warnings": warnings,
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

    def _policy_dict(self) -> dict[str, float]:
        return {
            "anonymous_threshold": self._policy.anonymous_threshold,
            "person_suggestion_threshold": self._policy.person_suggestion_threshold,
            "minimum_margin": self._policy.minimum_margin,
        }


def _compatible(
    vector: tuple[float, ...], candidates: tuple[tuple[str, tuple[float, ...]], ...]
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    return tuple(candidate for candidate in candidates if len(candidate[1]) == len(vector))


def _replace_reference(value: Any, old: str, new: str) -> Any:
    if value == old:
        return new
    if isinstance(value, dict):
        return {key: _replace_reference(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_reference(item, old, new) for item in value]
    return value


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("speaker identity timestamps require timezone information")
    return value.isoformat()


__all__ = ["SpeakerIdentityService"]
