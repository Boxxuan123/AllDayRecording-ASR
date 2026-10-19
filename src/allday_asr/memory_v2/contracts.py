"""Vendor-independent source, authorization and evidence contracts."""

from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
from typing import Protocol


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class SourceRecord:
    platform: str
    source_account_id: str
    conversation_id: str
    sender_account_id: str
    record_id: str
    revision: int
    sent_at: int
    timezone: str
    text: str
    raw_text: str
    is_from_self: bool | None
    speaker: str = ""
    conversation_name: str = ""
    deleted: bool = False
    source_locator: dict | None = None

    @property
    def key(self):
        return digest(
            [
                self.platform,
                self.source_account_id,
                self.conversation_id,
                self.record_id,
            ]
        )

    @property
    def actor(self):
        return (self.platform, self.source_account_id, self.sender_account_id)

    @property
    def fingerprint(self):
        return digest(asdict(self))

    @classmethod
    def native(cls, row):
        from allday_asr.v3.domain.chat_projection import semantic_text

        return cls(
            platform=row["platform"],
            source_account_id=row["source_account_id"],
            conversation_id=row["conversation_id"],
            sender_account_id=row.get("sender_account_id") or "",
            record_id=row["record_id"],
            revision=int(row["record_revision"]),
            sent_at=int(row["sent_at"]),
            timezone=row.get("timezone") or "Asia/Singapore",
            text=semantic_text(row.get("text") or ""),
            raw_text=row.get("text") or "",
            is_from_self=row.get("is_from_self"),
            speaker=row.get("sender_display_name") or "",
            conversation_name=row.get("conversation_display_name") or "",
            source_locator=row.get("source_locator"),
            deleted=bool(row.get("deleted")),
        )


@dataclass(frozen=True)
class Grant:
    """Caller-owned hard bounds; model exploration may never widen these."""

    accounts: frozenset[tuple[str, str]]
    start: int
    end: int
    conversations: frozenset[tuple[str, str, str]] | None = None

    def allows(self, row: SourceRecord):
        return (
            not row.deleted
            and (row.platform, row.source_account_id) in self.accounts
            and self.start <= row.sent_at < self.end
            and (
                self.conversations is None
                or (row.platform, row.source_account_id, row.conversation_id)
                in self.conversations
            )
        )


class EmbeddingPort(Protocol):
    async def encode_query(self, texts: list[str]): ...
    async def encode_documents(self, texts: list[str]): ...


class RerankingPort(Protocol):
    async def predict(self, pairs: list[tuple[str, str]]): ...


class MemoryBackend(Protocol):
    bank: str
    embeddings: EmbeddingPort
    reranker: RerankingPort

    def record(self, phase: str, start: float, details: dict): ...
    async def ingest(self, episodes: list[dict]) -> dict: ...
    async def retrieve(
        self, query: str, reference: int, window: dict | None = None
    ) -> dict: ...
    async def delete(self, episode_id: str) -> None: ...


def strict_schema(schema):
    if isinstance(schema, list):
        return [strict_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    s = {
        k: strict_schema(v) for k, v in schema.items() if k not in ("default", "title")
    }
    if s.get("type") == "object" or "properties" in s:
        s["additionalProperties"] = False
        s["required"] = list(s.get("properties", {}))
    return s
