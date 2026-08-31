from __future__ import annotations

import secrets
import threading
from pathlib import Path

from allday_asr.interfaces.web.jobs import JobRegistry
from allday_asr.interfaces.web.use_cases.background import BackgroundJobUseCases
from allday_asr.interfaces.web.use_cases.evaluation import EvaluationUseCases
from allday_asr.interfaces.web.use_cases.media import MediaUseCases
from allday_asr.interfaces.web.use_cases.semantic import SemanticUseCases
from allday_asr.interfaces.web.use_cases.timeline import TimelineUseCases
from allday_asr.interfaces.web.use_cases.workspace import WorkspaceUseCases
from allday_asr.storage.database import Database


class WebApplication(
    WorkspaceUseCases,
    EvaluationUseCases,
    TimelineUseCases,
    SemanticUseCases,
    BackgroundJobUseCases,
    MediaUseCases,
):
    def __init__(
        self,
        database_path: Path,
        config_path: Path,
        *,
        token: str | None = None,
    ):
        self.database_path = database_path.resolve()
        self.config_path = config_path.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.job_registry = JobRegistry()
        self.workflow_job_lock = threading.Lock()
        self.workflow_jobs: dict[int, str] = {}
        self.truth_lock = threading.Lock()
        self.audio_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def database(self) -> Database:
        return Database.open(self.database_path)
