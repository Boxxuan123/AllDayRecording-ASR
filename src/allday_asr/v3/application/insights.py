from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Lock

from allday_asr.v3.ports.insight_generation import InsightModelGenerator
from allday_asr.v3.ports.repositories import UnitOfWork

from .insight_errors import InsightGenerationFailed, InsightGenerationUnavailable
from .insight_generation import InsightGenerationMixin
from .insight_inputs import observations_from_dict
from .insight_management import InsightManagementMixin
from .insight_projections import _utc_now
from .daily_events import DailyEventMixin

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class DailyInsightService(DailyEventMixin, InsightGenerationMixin, InsightManagementMixin):
    """Builds grounded V3.6 summaries without ever reading an older summary."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        generator: InsightModelGenerator | None,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._generator = generator
        self._now = now or _utc_now
        self._daily_refresh_lock = Lock()
        self._daily_refreshed_at = float('-inf')


__all__ = [
    "DailyInsightService",
    "InsightGenerationFailed",
    "InsightGenerationUnavailable",
    "observations_from_dict",
]
