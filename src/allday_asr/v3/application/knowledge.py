from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from allday_asr.v3.ports.repositories import UnitOfWork

from .knowledge_generation import KnowledgeGenerationMixin
from .knowledge_queries import KnowledgeQueryMixin
from .knowledge_resolution import KnowledgeResolutionMixin
from .knowledge_support import ProposalResolution, _utc_now, cascade_derivations

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class KnowledgeArchitectureService(
    KnowledgeGenerationMixin, KnowledgeQueryMixin, KnowledgeResolutionMixin
):
    """V3.2 command boundary for derived event and memory data."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or _utc_now


__all__ = ["KnowledgeArchitectureService", "ProposalResolution", "cascade_derivations"]
