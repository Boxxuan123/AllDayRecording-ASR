"""Composition root for existing SQLite, Mac and model adapters."""

from allday_asr.v3.application.history_recall import HistoryRecall
from allday_asr.v3.adapters.sqlite.source_identity_repository import SourceIdentities
from allday_asr.v3.adapters.sqlite.recall_sources import RecallSources
from allday_asr.v3.adapters.chat_data.mac import MacChatData
from allday_asr.v3.adapters.codex.recall_model import RecallModelAdapter


def compose_history_recall(database, root, source=None, model=None, planner=None):
    identities = SourceIdentities(database, root / "chat/cache.sqlite3")
    from allday_asr.v3.adapters.sqlite.recall_directory import RecallDirectory
    from allday_asr.v3.adapters.codex.recall_planner import RecallPlannerAdapter

    selected_source = (
        source
        if source is not None
        else MacChatData(config_path=root / "chat/connection.json", timeout=3)
    )
    selected_planner = (
        planner
        if planner is not None
        else (RecallPlannerAdapter(root) if model is None else None)
    )
    return HistoryRecall(
        identities,
        RecallSources(identities),
        selected_source,
        model if model is not None else RecallModelAdapter(root),
        root=root,
        directory=RecallDirectory(identities, selected_source, root),
        planner=selected_planner,
    )
