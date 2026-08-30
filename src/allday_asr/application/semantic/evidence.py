from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

NON_LEXICAL_CHARS = frozenset("嗯啊哦呃额唔哎诶欸唉哼哈呀嘛喂")


@dataclass(frozen=True)
class ConversationEvidenceSettings:
    conversation_gap_ms: int = 180_000
    utterance_gap_ms: int = 2_500
    min_informative_chars: int = 4
    max_llm_request_chars: int = 200_000
    chunk_overlap_utterances: int = 6
    review_clip_ms: int = 120_000
    transcript_preview_chars: int = 600

    def validate(self) -> None:
        if not 30_000 <= self.conversation_gap_ms <= 900_000:
            raise ValueError("conversation_gap_ms 必须在 30 秒到 15 分钟之间")
        if not 250 <= self.utterance_gap_ms <= 30_000:
            raise ValueError("utterance_gap_ms 必须在 250 毫秒到 30 秒之间")
        if not 1 <= self.min_informative_chars <= 100:
            raise ValueError("min_informative_chars 必须在 1 到 100 之间")
        if self.max_llm_request_chars < 4_000:
            raise ValueError("max_llm_request_chars 不能小于 4000")
        if not 0 <= self.chunk_overlap_utterances <= 50:
            raise ValueError("chunk_overlap_utterances 必须在 0 到 50 之间")
        if not 10_000 <= self.review_clip_ms <= 120_000:
            raise ValueError("review_clip_ms 必须在 10 秒到 120 秒之间")
        if not 100 <= self.transcript_preview_chars <= 4_000:
            raise ValueError("transcript_preview_chars 必须在 100 到 4000 之间")


def build_conversation_evidence(
    tokens: Sequence[dict[str, Any]],
    disagreement_rows: Sequence[Any],
    *,
    settings: ConversationEvidenceSettings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not tokens:
        return [], []
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    ordered = sorted(tokens, key=lambda item: (item["start_ms"], item["id"]))
    for token in ordered:
        if current:
            gap_ms = int(token["start_ms"]) - int(current[-1]["end_ms"])
            if gap_ms > settings.conversation_gap_ms:
                groups.append(current)
                current = []
        current.append(dict(token))
    if current:
        groups.append(current)

    conversations: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for group_index, group in enumerate(groups):
        informative_chars = sum(
            _informative_char_count(str(token["text"])) for token in group
        )
        eligible = informative_chars >= settings.min_informative_chars
        target = conversations if eligible else excluded
        prefix = "conversation" if eligible else "excluded"
        key = f"{prefix}-{len(target) + 1:04d}"
        start_ms = int(group[0]["start_ms"])
        end_ms = int(group[-1]["end_ms"])
        transcript = "".join(str(token["text"]) for token in group).strip()
        source_refs = _deduplicate_source_refs(
            source_ref
            for token in group
            for source_ref in token["source_refs"]
        )
        alternatives = _conversation_disagreements(
            disagreement_rows, start_ms, end_ms
        )
        speaker_counts = Counter(
            str(token["speaker"])
            for token in group
            if token.get("speaker")
        )
        previous_gap = (
            start_ms - int(groups[group_index - 1][-1]["end_ms"])
            if group_index > 0
            else None
        )
        next_gap = (
            int(groups[group_index + 1][0]["start_ms"]) - end_ms
            if group_index + 1 < len(groups)
            else None
        )
        uncertainty = {
            "unassigned_tokens": sum(
                token["speaker_kind"] == "none" for token in group
            ),
            "uncertain_speaker_tokens": sum(
                token["speaker_kind"] == "uncertain" for token in group
            ),
            "overlap_tokens": sum(bool(token["has_overlap"]) for token in group),
            "asr_disagreement_windows": len(alternatives),
        }
        target.append(
            {
                "key": key,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "duration_ms": end_ms - start_ms,
                "transcript": transcript,
                "transcript_preview": _truncate(
                    transcript, settings.transcript_preview_chars
                ),
                "token_count": len(group),
                "informative_char_count": informative_chars,
                "cloud_eligible": eligible,
                "exclusion_reason": (
                    None if eligible else "insufficient_lexical_content"
                ),
                "boundary": {
                    "policy": "token-gap-conversation-candidate-v1",
                    "conversation_gap_ms": settings.conversation_gap_ms,
                    "previous_gap_ms": previous_gap,
                    "next_gap_ms": next_gap,
                    "hard_duration_cap_ms": None,
                },
                "speakers": [
                    {"label": label, "token_count": count}
                    for label, count in sorted(
                        speaker_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ],
                "uncertainty": uncertainty,
                "asr_alternatives": alternatives,
                "utterances": _build_utterances(
                    key,
                    group,
                    gap_ms=settings.utterance_gap_ms,
                ),
                "review_clips": _review_clips(
                    start_ms, end_ms, settings.review_clip_ms
                ),
                "evidence": {
                    "token_ids": [int(token["id"]) for token in group],
                    "source_refs": source_refs,
                },
                "tokens": group,
            }
        )
    return conversations, excluded


def _build_utterances(
    conversation_key: str,
    tokens: Sequence[dict[str, Any]],
    *,
    gap_ms: int,
) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    for token in tokens:
        speaker = str(token.get("speaker") or "unassigned")
        if groups:
            previous = groups[-1][-1]
            previous_speaker = str(previous.get("speaker") or "unassigned")
            gap = int(token["start_ms"]) - int(previous["end_ms"])
            if speaker == previous_speaker and gap <= gap_ms:
                groups[-1].append(dict(token))
                continue
        groups.append([dict(token)])
    utterances: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        source_refs = _deduplicate_source_refs(
            ref for token in group for ref in token["source_refs"]
        )
        utterances.append(
            {
                "key": f"{conversation_key}:utterance-{index:04d}",
                "start_ms": int(group[0]["start_ms"]),
                "end_ms": int(group[-1]["end_ms"]),
                "speaker": str(group[0].get("speaker") or "unassigned"),
                "speaker_kind": str(group[0].get("speaker_kind") or "none"),
                "text": "".join(str(item["text"]) for item in group),
                "uncertainty": {
                    "unassigned_tokens": sum(
                        item["speaker_kind"] == "none" for item in group
                    ),
                    "uncertain_speaker_tokens": sum(
                        item["speaker_kind"] == "uncertain" for item in group
                    ),
                    "overlap_tokens": sum(
                        bool(item["has_overlap"]) for item in group
                    ),
                },
                "evidence": {
                    "token_ids": [int(item["id"]) for item in group],
                    "source_refs": source_refs,
                },
            }
        )
    return utterances


def _conversation_disagreements(
    rows: Sequence[Any], start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for row in rows:
        row_start = int(row["session_start_ms"])
        row_end = int(row["session_end_ms"])
        if row_end <= start_ms or row_start >= end_ms:
            continue
        details = _json_object(row["details_json"])
        values.append(
            {
                "key": f"asr-window-{int(row['window_index']):04d}",
                "start_ms": max(start_ms, row_start),
                "end_ms": min(end_ms, row_end),
                "priority": str(row["priority"]),
                "normalized_distance": float(row["normalized_distance"]),
                "primary_text": str(details.get("primary_text") or ""),
                "secondary_text": str(details.get("secondary_text") or ""),
                "scope": "window_level_alternative_not_aligned_claim",
            }
        )
    return sorted(values, key=lambda item: (item["start_ms"], item["key"]))


def _review_clips(
    start_ms: int, end_ms: int, clip_ms: int
) -> list[dict[str, Any]]:
    clips: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor < end_ms:
        clip_end = min(end_ms, cursor + clip_ms)
        clips.append(
            {
                "index": len(clips) + 1,
                "start_ms": cursor,
                "end_ms": clip_end,
            }
        )
        cursor = clip_end
    return clips


def _informative_char_count(text: str) -> int:
    characters = [char for char in text.strip() if char.isalnum()]
    if not characters or all(char in NON_LEXICAL_CHARS for char in characters):
        return 0
    return len(characters)


def _deduplicate_source_refs(
    refs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None, str, int, int]] = set()
    for ref in refs:
        key = (
            int(ref["source_object_id"]),
            (
                int(ref["source_instance_id"])
                if ref.get("source_instance_id") is not None
                else None
            ),
            str(ref["source_sha256"]),
            int(ref["source_start_ms"]),
            int(ref["source_end_ms"]),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "source_object_id": key[0],
                "source_instance_id": key[1],
                "source_sha256": key[2],
                "source_start_ms": key[3],
                "source_end_ms": key[4],
            }
        )
    return output


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"
