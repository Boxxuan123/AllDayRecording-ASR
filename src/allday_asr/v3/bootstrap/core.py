from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from allday_asr.v3.paths import PROJECT_ROOT
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.audio.review_cache import ReviewAudioCacheOwner
from allday_asr.v3.adapters.codex import (
    CodexInsightGenerator,
    CodexReminderGenerator,
    CodexSemanticEventGenerator,
)
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import (
    AdmissionService,
    CorrectionInvalidationService,
    DailyInsightService,
    DurableProcessingService,
    DesktopQueryService,
    IntelligentReminderService,
    KnowledgeArchitectureService,
    MobileSyncService,
    PersonMemoryService,
    ReminderExtractionService,
    SemanticEventExtractionService,
    SpeakerIdentityService,
    UtteranceCorrectionOperationHandler,
)
from allday_asr.v3.config import CodexReminderSettings
from allday_asr.v3.ports.reminder_generation import ReminderModelGenerator
from allday_asr.v3.ports.insight_generation import InsightModelGenerator
from allday_asr.v3.ports.event_generation import SemanticEventModelGenerator
from allday_asr.v3.ports.speaker_embeddings import SpeakerEmbeddingProvider
from allday_asr.v3.ports.self_identity_matching import SelfIdentityMatcher


@dataclass(frozen=True)
class V3CorePaths:
    state_dir: Path
    database_path: Path
    audio_store_path: Path
    artifact_store_path: Path

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> V3CorePaths:
        root = state_dir.resolve()
        return cls(
            state_dir=root,
            database_path=root / "core.sqlite3",
            audio_store_path=root / "audio",
            artifact_store_path=root / "artifacts",
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> V3CorePaths:
        values = os.environ if environ is None else environ
        state_dir = Path(
            values.get("ALLDAY_V3_STATE_DIR", project_root / "state" / "v3")
        )
        return cls.from_state_dir(state_dir)


@dataclass(frozen=True)
class V3Core:
    paths: V3CorePaths
    database: V3Database
    audio_store: ContentAddressedStore
    artifact_store: ContentAddressedStore
    mobile_sync: MobileSyncService
    admission: AdmissionService
    processing: DurableProcessingService
    corrections: CorrectionInvalidationService
    desktop: DesktopQueryService
    knowledge: KnowledgeArchitectureService
    reminders: IntelligentReminderService
    reminder_extraction: ReminderExtractionService
    semantic_events: SemanticEventExtractionService
    people: SpeakerIdentityService
    person_memory: PersonMemoryService
    insights: DailyInsightService
    review_audio_cache: ReviewAudioCacheOwner = field(default_factory=ReviewAudioCacheOwner)

    def initialize(self) -> int:
        """Create only V3-owned state and migrate it to the latest schema."""
        version = self.database.initialize()
        self.audio_store.initialize()
        self.artifact_store.initialize()
        return version

    def close(self) -> None:
        self.review_audio_cache.close()
        self.people.sample_worker.close()
        self.semantic_events.close()
        self.reminder_extraction.close()
        self.insights.close()


def compose_v3_core(
    paths: V3CorePaths | None = None,
    *,
    codex_settings: CodexReminderSettings | None = None,
    reminder_generator: ReminderModelGenerator | None = None,
    semantic_event_generator: SemanticEventModelGenerator | None = None,
    insight_generator: InsightModelGenerator | None = None,
    speaker_embedding_provider: SpeakerEmbeddingProvider | None = None,
    self_identity_matcher: SelfIdentityMatcher | None = None,
) -> V3Core:
    """Wire the V3 Core without opening databases or creating directories."""
    selected = paths or V3CorePaths.from_environment()
    selected_codex = codex_settings or CodexReminderSettings.from_environment()
    database = V3Database(selected.database_path)
    audio_store = ContentAddressedStore(selected.audio_store_path)
    artifact_store = ContentAddressedStore(selected.artifact_store_path)
    admission = AdmissionService(lambda: SqliteUnitOfWork(database))
    processing = DurableProcessingService(
        lambda: SqliteUnitOfWork(database), artifact_store
    )
    corrections = CorrectionInvalidationService(
        lambda: SqliteUnitOfWork(database)
    )
    knowledge = KnowledgeArchitectureService(lambda: SqliteUnitOfWork(database))
    speaker_provider = speaker_embedding_provider or FunASRSpeakerEmbeddingProvider(
        audio_store,
        device=os.environ.get("ALLDAY_V3_SPEAKER_DEVICE", "auto"),
        temp_root=selected.state_dir / "speaker-temp",
    )
    people = SpeakerIdentityService(
        lambda: SqliteUnitOfWork(database),
        speaker_provider,
        knowledge,
        self_identity_matcher=(
            self_identity_matcher
            if self_identity_matcher is not None
            else CalibratedSelfIdentityMatcher(selected.state_dir)
        ),
    )
    mobile_sync = MobileSyncService(
        lambda: SqliteUnitOfWork(database),
        operation_handler=UtteranceCorrectionOperationHandler(),
    )
    person_memory = PersonMemoryService(lambda: SqliteUnitOfWork(database))
    reminders = IntelligentReminderService(
        lambda: SqliteUnitOfWork(database), knowledge
    )
    generator = reminder_generator
    if generator is None and selected_codex.enabled:
        generator = CodexReminderGenerator(
            selected_codex.workdir,
            model=selected_codex.model,
        )
    event_generator = semantic_event_generator
    if event_generator is None and selected_codex.enabled:
        event_generator = CodexSemanticEventGenerator(
            selected_codex.workdir,
            model=selected_codex.model,
        )
    narrative_generator = insight_generator
    if narrative_generator is None and selected_codex.enabled:
        narrative_generator = CodexInsightGenerator(
            selected_codex.workdir,
            model=selected_codex.model,
        )
    desktop = DesktopQueryService(
        lambda: SqliteUnitOfWork(database),
        codex_reminders_enabled=generator is not None,
        codex_insights_enabled=narrative_generator is not None,
        codex_semantic_events_enabled=event_generator is not None,
    )
    reminder_extraction = ReminderExtractionService(
        lambda: SqliteUnitOfWork(database),
        reminders,
        generator,
        default_effort=selected_codex.reasoning_effort.value,
        allow_auto_apply=selected_codex.allow_auto_apply,
    )
    semantic_events = SemanticEventExtractionService(
        lambda: SqliteUnitOfWork(database),
        knowledge,
        event_generator,
        default_effort=selected_codex.reasoning_effort.value,
        allow_auto_accept=selected_codex.allow_semantic_event_auto_accept,
    )
    insights = DailyInsightService(
        lambda: SqliteUnitOfWork(database), narrative_generator
    )
    return V3Core(
        paths=selected,
        database=database,
        audio_store=audio_store,
        artifact_store=artifact_store,
        mobile_sync=mobile_sync,
        admission=admission,
        processing=processing,
        corrections=corrections,
        desktop=desktop,
        knowledge=knowledge,
        reminders=reminders,
        reminder_extraction=reminder_extraction,
        semantic_events=semantic_events,
        people=people,
        person_memory=person_memory,
        insights=insights,
    )


__all__ = ["V3Core", "V3CorePaths", "compose_v3_core"]
