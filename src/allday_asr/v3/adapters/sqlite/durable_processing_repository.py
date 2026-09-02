import sqlite3

from .durable_processing_execution import DurableProcessingExecutionMixin
from .durable_processing_lifecycle import DurableProcessingLifecycleMixin
from .durable_processing_queries import DurableProcessingQueryMixin
from .repositories import Clock


class SqliteDurableProcessingRepository(
    DurableProcessingQueryMixin,
    DurableProcessingExecutionMixin,
    DurableProcessingLifecycleMixin,
):
    def __init__(self, connection: sqlite3.Connection, *, now: Clock) -> None:
        self.connection = connection
        self.now = now
