from __future__ import annotations

from typing import Any, Protocol

from allday_asr.v3.domain.identity import IdentityDecision
from allday_asr.v3.domain.people import SpeakerEmbedding


class SelfIdentityMatcher(Protocol):
    """Match a speaker embedding against an explicitly enrolled self voiceprint."""

    def status(self) -> dict[str, Any]: ...

    def match(self, embedding: SpeakerEmbedding) -> IdentityDecision: ...


__all__ = ["SelfIdentityMatcher"]
