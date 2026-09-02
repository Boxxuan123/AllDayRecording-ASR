from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from allday_asr.v3.ports.repositories import UnitOfWork

from .person_memory_commands import PersonMemoryCommandMixin
from .person_memory_refresh import PersonMemoryRefreshMixin
from .person_memory_support import memory_draft_from_dict, revision_from_dict

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class PersonMemoryService(PersonMemoryRefreshMixin, PersonMemoryCommandMixin):
    """Versioned, evidence-linked memory for stable people across sessions."""

    def __init__(
        self, uow_factory: UnitOfWorkFactory, *, now: DateTimeClock | None = None
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(timezone.utc))

__all__ = ["PersonMemoryService", "memory_draft_from_dict", "revision_from_dict"]
