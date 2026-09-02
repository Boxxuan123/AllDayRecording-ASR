from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from allday_asr.v3.application.knowledge import KnowledgeArchitectureService
from allday_asr.v3.ports.repositories import UnitOfWork

from .reminder_apply import ReminderApplyMixin
from .reminder_commands import ReminderCommandMixin
from .reminder_queries import ReminderQueryMixin
from .reminder_support import AUTO_APPLY_CONFIDENCE, _utc_now

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class IntelligentReminderService(
    ReminderCommandMixin, ReminderQueryMixin, ReminderApplyMixin
):
    """V3.3 reminder policy layered on V3.2 proposals and event operations."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        knowledge: KnowledgeArchitectureService,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._knowledge = knowledge
        self._now = now or _utc_now


__all__ = ["AUTO_APPLY_CONFIDENCE", "IntelligentReminderService"]
