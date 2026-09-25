from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from allday_asr.v3.domain.people import ClusterMatchPolicy
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.self_identity_matching import SelfIdentityMatcher
from allday_asr.v3.ports.speaker_embeddings import SpeakerEmbeddingProvider

from .knowledge import KnowledgeArchitectureService
from .people_clusters import PeopleClusterMixin
from .people_identity import PeopleIdentityMixin
from .people_prototypes import PeoplePrototypeMixin

UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]


class SpeakerIdentityService(
    PeopleIdentityMixin, PeoplePrototypeMixin, PeopleClusterMixin
):
    """Open-set identity workflow with calibrated automatic self recognition."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_provider: SpeakerEmbeddingProvider,
        knowledge: KnowledgeArchitectureService,
        *,
        policy: ClusterMatchPolicy | None = None,
        self_identity_matcher: SelfIdentityMatcher | None = None,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = embedding_provider
        self._knowledge = knowledge
        self._policy = policy or ClusterMatchPolicy()
        self._self_identity_matcher = self_identity_matcher
        self._now = now or (lambda: datetime.now(timezone.utc))
        from .annotation_samples import AnnotationSampleWorker
        self.sample_worker = AnnotationSampleWorker(self)


__all__ = ["SpeakerIdentityService"]
