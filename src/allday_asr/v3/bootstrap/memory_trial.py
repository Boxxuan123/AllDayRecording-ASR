"""Wire trial input selection and persistence at the composition root."""

import time
from allday_asr.v3.application.memory_trial import MemoryTrial
from allday_asr.v3.application.cross_day_memory import CrossDayMemoryService
from allday_asr.v3.adapters.sqlite.memory_trial_sources import MemoryTrialSources
from allday_asr.v3.adapters.sqlite.memory_trial_store import SqliteMemoryTrialStore


def compose_memory_trial(chat, memory, *, clock=time.time):
    database = memory.sources.database
    processor = CrossDayMemoryService(
        MemoryTrialSources(database, memory.uow_factory, chat.cache), memory.uow_factory
    )
    return MemoryTrial(
        chat,
        memory,
        processor,
        SqliteMemoryTrialStore(chat.cache, database),
        clock=clock,
    )
