"""Compose chat adapters without adding another application server."""

from pathlib import Path

from allday_asr.v3.adapters.chat_data.answer import evidence_answer
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.adapters.sqlite.chat_cache import ChatCache
from allday_asr.v3.application.chat_queries import ChatQueries


def compose_chat_queries(root: Path, source=None, model=None, *, start_worker=True):
    return ChatQueries(
        root,
        source or MacChatData(config_path=root / "connection.json"),
        ChatCache(root / "cache.sqlite3"),
        evidence_answer,
        model,
        start_worker=start_worker,
    )
