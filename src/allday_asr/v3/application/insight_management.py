from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.ports.insight_generation import (
    InsightModelGenerator,
)

from .insight_errors import InsightGenerationFailed, InsightGenerationUnavailable
from .insight_projections import _datetime
from .insight_validation import (
    _copy_report_evidence,
    _relationship_copy_values,
    _report_evidence_ids,
    _validated_observations,
)


class InsightManagementMixin:
    def relationships(
        self, person_id: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if not 1 <= limit <= 500:
            raise ValueError("relationship observation limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.insights.list_relationships(person_id, limit)

    def relationship(self, report_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.insights.relationship(report_id)

    def revise_relationship(
        self, report_id: str, raw_observations: Iterable[dict[str, Any]]
    ) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            event_ids, utterance_ids = _report_evidence_ids(current)
            try:
                observations = _validated_observations(
                    tuple(raw_observations), event_ids, utterance_ids
                )
            except InsightGenerationFailed as exc:
                raise ValueError(str(exc)) from exc
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=observations,
                    status="active",
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                "revise",
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
            )
            uow.audit.append(
                "insight.relationship.revised",
                "desktop-user",
                "relationship_observation",
                report_id,
                {"revision": revision, "before_revision": current["revision"]},
            )
            return uow.insights.relationship(report_id)

    def retract_relationship(self, report_id: str) -> dict[str, Any]:
        return self._change_relationship_status(report_id, "retracted", "retract")

    def undo_relationship(self, report_id: str) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            operation = uow.insights.latest_relationship_operation(report_id)
            if operation is None or operation["kind"] != "retract":
                raise ValueError("latest relationship operation cannot be undone")
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=current["observations"],
                    status="active",
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                "restore",
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
                reverts_operation_id=operation["operation_id"],
            )
            return uow.insights.relationship(report_id)

    def close(self) -> None:
        if self._generator is not None:
            self._generator.close()

    def _change_relationship_status(
        self, report_id: str, status: str, operation_kind: str
    ) -> dict[str, Any]:
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.insights.relationship(report_id)
            if current["status"] == status:
                raise ValueError(f"relationship observation is already {status}")
            revision = uow.insights.next_relationship_revision(report_id)
            uow.insights.add_relationship(
                **_relationship_copy_values(
                    current,
                    revision,
                    observations=current["observations"],
                    status=status,
                    created_by="desktop-user",
                    created_at=_datetime(now),
                )
            )
            _copy_report_evidence(uow, current, revision, now)
            uow.insights.add_operation(
                new_ulid(),
                "relationship_observation",
                report_id,
                revision,
                operation_kind,
                "desktop-user",
                {"before_revision": current["revision"]},
                _datetime(now),
            )
            return uow.insights.relationship(report_id)

    def _require_generator(self) -> InsightModelGenerator:
        if self._generator is None:
            raise InsightGenerationUnavailable("Codex insight generation is disabled")
        return self._generator
