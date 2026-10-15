from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from threading import Lock

from allday_asr.v3.ports.insight_generation import InsightModelGenerator
from allday_asr.v3.ports.daily_semantics import DailySemanticAnalyzer
from allday_asr.v3.ports.repositories import UnitOfWork

from .insight_errors import InsightGenerationFailed, InsightGenerationUnavailable
from .insight_generation import InsightGenerationMixin
from .insight_inputs import observations_from_dict
from .insight_management import InsightManagementMixin
from .insight_projections import _utc_now
from .daily_events import DailyEventMixin

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class DailyInsightService(
    DailyEventMixin, InsightGenerationMixin, InsightManagementMixin
):
    """Builds grounded V3.6 summaries without ever reading an older summary."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        generator: InsightModelGenerator | None,
        *,
        now: DateTimeClock | None = None,
        daily_analyzer: DailySemanticAnalyzer | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._generator = generator
        self._now = now or _utc_now
        self._daily_analyzer = daily_analyzer
        self._daily_refresh_lock = Lock()
        self._daily_refreshed_at = float("-inf")

    @property
    def generation_version(self):
        from .daily_inventory import generation_version
        return generation_version(self._daily_analyzer)

    @property
    def daily_model_label(self):
        return getattr(self._daily_analyzer, 'model_label', 'local-disabled')

    @property
    def daily_execution_observation(self):
        analyzer = self._daily_analyzer
        return {'receipt_dir': getattr(analyzer, 'receipt_dir', None),
                'remote_starts': getattr(analyzer, 'remote_starts', 0),
                'cache_hits': len(getattr(analyzer, 'cache_hits', []))}

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._daily_analyzer is not None:
                self._daily_analyzer.close()


__all__ = [
    "DailyInsightService",
    "InsightGenerationFailed",
    "InsightGenerationUnavailable",
    "observations_from_dict",
]
