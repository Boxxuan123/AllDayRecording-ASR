from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from allday_asr.application.semantic.evidence import build_conversation_evidence
from allday_asr.application.semantic.inputs import (
    resolve_semantic_input_runs,
    semantic_tokens,
)
from allday_asr.domain.hashing import canonical_json as _canonical_json
from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_json
from allday_asr.paths import OUTPUT_DIR
from allday_asr.application.semantic.legacy.v2e0 import (
    semantic_overview as legacy_semantic_overview,
)
from allday_asr.storage.database import Database

EVIDENCE_LEDGER_FORMAT = "AllDayRecording local semantic evidence ledger v2"
LLM_TRANSPORT_FORMAT = "AllDayRecording LLM transport plan v1"
LLM_REQUEST_FORMAT = "AllDayRecording LLM semantic request v1"
SEMANTIC_RESPONSE_FORMAT = "AllDayRecording semantic candidate response v2"

NON_LEXICAL_CHARS = frozenset("嗯啊哦呃额唔哎诶欸唉哼哈呀嘛喂")
FORBIDDEN_PROVIDER_KEYS = frozenset(
    {
        "audio",
        "audio_bytes",
        "source_object_id",
        "source_instance_id",
        "source_path",
        "source_refs",
        "source_sha256",
        "token_id",
        "token_ids",
        "input_fingerprint",
        "session_id",
        "recording_id",
    }
)


@dataclass(frozen=True)
class SemanticV2E01Settings:
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


@dataclass(frozen=True)
class SemanticV2E01Summary:
    run_id: int
    asr_run_id: int
    diarization_run_id: int | None
    conversation_count: int
    excluded_block_count: int
    llm_job_count: int
    llm_payload_bytes: int
    token_count: int
    candidate_count: int
    request_sha256: str
    provider_request_sha256: str
    response_sha256: str
    manifest_path: Path

    @property
    def event_count(self) -> int:
        """Compatibility alias for older CLI/web callers."""
        return self.conversation_count


class SemanticProvider(Protocol):
    provider_name: str
    model_name: str
    network_access: bool

    def generate(self, request: dict[str, Any]) -> dict[str, Any]: ...


class DeterministicMockSemanticProvider:
    """Local contract provider that sees only the minimized LLM payload."""

    provider_name = "local_mock"
    model_name = "conversation-contract-v2"
    network_access = False

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        conversations = _unique_transport_conversations(request)
        duration_ms = int(request["session"]["duration_ms"])
        first_start = (
            int(conversations[0]["start_ms"]) if conversations else 0
        )
        last_end = (
            int(conversations[-1]["end_ms"])
            if conversations
            else duration_ms
        )
        keys = [str(item["key"]) for item in conversations]
        return {
            "format": SEMANTIC_RESPONSE_FORMAT,
            "mode": "local_mock_conversation_contract",
            "summary": {
                "key": "daily-summary",
                "start_ms": first_start,
                "end_ms": last_end,
                "title": "V2-E.0.1 完整对话传输计划",
                "body": (
                    f"已形成 {len(conversations)} 个完整对话候选和 "
                    f"{len(request['jobs'])} 个计划请求。当前是本地 mock，"
                    "没有生成日记事实，也没有调用云服务。"
                ),
                "evidence_conversation_keys": keys,
            },
            "conversations": [
                {
                    "key": str(conversation["key"]),
                    "start_ms": int(conversation["start_ms"]),
                    "end_ms": int(conversation["end_ms"]),
                    "title": (
                        f"完整对话候选 {index:02d} · "
                        f"{_format_offset(int(conversation['start_ms']))}"
                    ),
                    "body": (
                        "整段对话将作为同一语义上下文；120 秒仅用于本地回听。"
                        "待云端语义模型处理。文字预览："
                        + _truncate(str(conversation["transcript"]), 600)
                    ),
                    "evidence_utterance_keys": [
                        str(item["key"])
                        for item in conversation["utterances"]
                    ],
                }
                for index, conversation in enumerate(conversations, start=1)
            ],
            "facts": [],
            "actions": [],
            "limitations": [
                "No cloud or local LLM was called.",
                "Conversation titles are time labels, not semantic claims.",
                "Facts and actions intentionally remain empty in mock mode.",
            ],
        }


def run_semantic_v2e01(
    database: Database,
    recording_id: int,
    *,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
    settings: SemanticV2E01Settings | None = None,
    provider: SemanticProvider | None = None,
) -> SemanticV2E01Summary:
    """Freeze full conversations and a separate minimized provider payload."""
    settings = settings or SemanticV2E01Settings()
    settings.validate()
    provider = provider or DeterministicMockSemanticProvider()
    if provider.network_access:
        raise ValueError("V2-E.0.1 只允许不联网 provider")

    asr_run, diarization_run = resolve_semantic_input_runs(
        database,
        recording_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
    )
    session = database.get_session_for_recording(recording_id)
    session_id = int(session["id"])
    tokens = semantic_tokens(
        database,
        int(asr_run["id"]),
        int(diarization_run["id"]) if diarization_run is not None else None,
    )
    if not tokens:
        raise RuntimeError("V2-E.0.1 没有可用的 committed ASR token")
    disagreements = database.list_asr_disagreements(int(asr_run["id"]))
    conversations, excluded_blocks = build_conversation_evidence(
        tokens,
        disagreements,
        settings=settings,
    )
    provider_request = build_provider_request(
        session,
        conversations,
        settings=settings,
    )
    _validate_provider_privacy(provider_request)
    request = build_evidence_ledger(
        database,
        recording_id,
        session_id=session_id,
        asr_run_id=int(asr_run["id"]),
        diarization_run_id=(
            int(diarization_run["id"])
            if diarization_run is not None
            else None
        ),
        conversations=conversations,
        excluded_blocks=excluded_blocks,
        provider_request=provider_request,
        settings=settings,
    )
    request_sha256 = _sha256_json(request)
    provider_request_sha256 = _sha256_json(provider_request)
    config = {
        "evidence_format": EVIDENCE_LEDGER_FORMAT,
        "provider_request_format": LLM_TRANSPORT_FORMAT,
        "response_format": SEMANTIC_RESPONSE_FORMAT,
        "asr_run_id": int(asr_run["id"]),
        "diarization_run_id": (
            int(diarization_run["id"])
            if diarization_run is not None
            else None
        ),
        "provider": provider.provider_name,
        "model": provider.model_name,
        **asdict(settings),
        "request_sha256": request_sha256,
        "provider_request_sha256": provider_request_sha256,
        "privacy": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_receives_source_hashes": False,
            "provider_receives_token_database_ids": False,
        },
    }
    run_id = database.start_processing_run(
        recording_id,
        run_kind="semantic_v2e0",
        config=config,
        config_sha256=_sha256_json(config),
        model_manifest={
            "provider": provider.provider_name,
            "model": provider.model_name,
            "network_access": False,
            "audio_uploaded": False,
            "text_uploaded": False,
            "provider_payload_sha256": provider_request_sha256,
        },
        pipeline_version="v2-e.0.1",
        parent_run_id=(
            int(diarization_run["id"])
            if diarization_run is not None
            else int(asr_run["id"])
        ),
    )
    try:
        response = provider.generate(provider_request)
        _validate_response(
            response,
            conversations,
            duration_ms=int(session["duration_ms"]),
        )
        response_sha256 = _sha256_json(response)
        candidates = response_candidates(run_id, response, conversations)
        database.create_semantic_snapshot(
            run_id,
            {
                "asr_run_id": int(asr_run["id"]),
                "diarization_run_id": (
                    int(diarization_run["id"])
                    if diarization_run is not None
                    else None
                ),
                "provider": provider.provider_name,
                "model": provider.model_name,
                "request_format": EVIDENCE_LEDGER_FORMAT,
                "response_format": SEMANTIC_RESPONSE_FORMAT,
                "request": request,
                "response": response,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "audio_bytes_included": False,
                "source_paths_included": False,
            },
            candidates,
        )
        provider_payload_bytes = len(
            _canonical_json(provider_request).encode("utf-8")
        )
        summary_payload = {
            "session_id": session_id,
            "asr_run_id": int(asr_run["id"]),
            "diarization_run_id": (
                int(diarization_run["id"])
                if diarization_run is not None
                else None
            ),
            "provider": provider.provider_name,
            "model": provider.model_name,
            "mode": "local_mock_conversation_contract",
            "conversation_count": len(conversations),
            "excluded_block_count": len(excluded_blocks),
            "llm_job_count": len(provider_request["jobs"]),
            "llm_payload_bytes": provider_payload_bytes,
            "token_count": len(tokens),
            "candidate_count": len(candidates),
            "request_sha256": request_sha256,
            "provider_request_sha256": provider_request_sha256,
            "response_sha256": response_sha256,
            "facts": 0,
            "actions": 0,
            "privacy": {
                "network_access": False,
                "audio_bytes_included": False,
                "source_paths_included": False,
                "biometric_data_included": False,
                "provider_receives_source_hashes": False,
                "provider_receives_token_database_ids": False,
            },
        }
        manifest_path = _write_manifest(
            run_id,
            session_id,
            config,
            request,
            provider_request,
            response,
            summary_payload,
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        return SemanticV2E01Summary(
            run_id=run_id,
            asr_run_id=int(asr_run["id"]),
            diarization_run_id=(
                int(diarization_run["id"])
                if diarization_run is not None
                else None
            ),
            conversation_count=len(conversations),
            excluded_block_count=len(excluded_blocks),
            llm_job_count=len(provider_request["jobs"]),
            llm_payload_bytes=provider_payload_bytes,
            token_count=len(tokens),
            candidate_count=len(candidates),
            request_sha256=request_sha256,
            provider_request_sha256=provider_request_sha256,
            response_sha256=response_sha256,
            manifest_path=manifest_path,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def build_provider_request(
    session: Any,
    conversations: Sequence[dict[str, Any]],
    *,
    settings: SemanticV2E01Settings,
) -> dict[str, Any]:
    compact = [_compact_conversation(item) for item in conversations]
    jobs = _pack_llm_jobs(compact, settings=settings)
    request = {
        "format": LLM_TRANSPORT_FORMAT,
        "strategy": "whole-conversation-first-hierarchical-fallback-v1",
        "session": {
            "recorded_at": str(session["recorded_at"]),
            "timezone": str(session["timezone"]),
            "duration_ms": int(session["duration_ms"]),
        },
        "policy": {
            "conversation_boundaries_are_candidates": True,
            "hard_conversation_duration_cap_ms": None,
            "split_only_for_provider_context_limit": True,
            "chunk_overlap_utterances": settings.chunk_overlap_utterances,
            "review_clip_limit_is_not_semantic_boundary": True,
            "source_audio_available_to_provider": False,
        },
        "jobs": jobs,
    }
    _validate_provider_privacy(request)
    return request


def build_evidence_ledger(
    database: Database,
    recording_id: int,
    *,
    session_id: int,
    asr_run_id: int,
    diarization_run_id: int | None,
    conversations: Sequence[dict[str, Any]],
    excluded_blocks: Sequence[dict[str, Any]],
    provider_request: dict[str, Any],
    settings: SemanticV2E01Settings,
) -> dict[str, Any]:
    recording = database.get_recording(recording_id)
    session = database.get_recording_session(session_id)
    return {
        "format": EVIDENCE_LEDGER_FORMAT,
        "mode": "local_audit_ledger_plus_minimized_provider_payload",
        "session": {
            "session_id": session_id,
            "recorded_at": str(session["recorded_at"]),
            "timezone": str(session["timezone"]),
            "duration_ms": int(session["duration_ms"]),
            "device": recording["device"],
        },
        "inputs": {
            "asr_run_id": asr_run_id,
            "diarization_run_id": diarization_run_id,
            "input_fingerprint": database.session_input_fingerprint(session_id),
        },
        "boundary_contract": {
            "conversation_gap_ms": settings.conversation_gap_ms,
            "hard_conversation_duration_cap_ms": None,
            "review_clip_ms": settings.review_clip_ms,
            "review_clip_is_semantic_boundary": False,
            "provider_chunk_is_semantic_boundary": False,
        },
        "privacy": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_payload_contains_source_hashes": False,
            "provider_payload_contains_database_token_ids": False,
            "local_ledger_uses_source_hashes_for_audit": True,
        },
        "evidence_ledger": {
            "conversations": list(conversations),
            "excluded_blocks": list(excluded_blocks),
        },
        "llm_transport": provider_request,
        "output_contract": {
            "required": [
                "summary",
                "conversations",
                "facts",
                "actions",
            ],
            "claims_must_cite_conversation_and_utterance_keys": True,
            "may_overwrite_asr": False,
            "external_write_requires_human_confirmation": True,
        },
    }


def response_candidates(
    run_id: int,
    response: dict[str, Any],
    conversations: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_key = {str(item["key"]): item for item in conversations}
    summary = dict(response["summary"])
    candidates = [
        {
            "candidate_key": f"semantic:{run_id}:daily-summary",
            "candidate_type": "daily_summary",
            "session_start_ms": int(summary["start_ms"]),
            "session_end_ms": int(summary["end_ms"]),
            "title": str(summary["title"]),
            "body": str(summary["body"]),
            "confidence": None,
            "evidence": {
                "semantic_unit": "day",
                "conversation_keys": list(
                    summary["evidence_conversation_keys"]
                ),
                "mock_only": True,
            },
        }
    ]
    for item in response["conversations"]:
        source = by_key[str(item["key"])]
        candidates.append(
            {
                "candidate_key": f"semantic:{run_id}:{item['key']}",
                "candidate_type": "event",
                "session_start_ms": int(item["start_ms"]),
                "session_end_ms": int(item["end_ms"]),
                "title": str(item["title"]),
                "body": str(item["body"]),
                "confidence": None,
                "evidence": {
                    "semantic_unit": "conversation",
                    "conversation_key": str(item["key"]),
                    "token_ids": list(source["evidence"]["token_ids"]),
                    "source_refs": list(source["evidence"]["source_refs"]),
                    "utterance_keys": [
                        str(value["key"]) for value in source["utterances"]
                    ],
                    "uncertainty": dict(source["uncertainty"]),
                    "speaker_counts": list(source["speakers"]),
                    "review_clips": list(source["review_clips"]),
                    "asr_alternative_windows": len(
                        source["asr_alternatives"]
                    ),
                    "mock_only": True,
                },
            }
        )
    return candidates


def semantic_overview(database: Database, recording_id: int) -> dict[str, Any]:
    database.get_recording(recording_id)
    completed = [
        row
        for row in database.list_processing_runs(recording_id)
        if str(row["run_kind"]) == "semantic_v2e0"
        and str(row["status"]) == "completed"
    ]
    if not completed:
        return legacy_semantic_overview(database, recording_id)
    run = completed[-1]
    exchange = database.get_semantic_exchange(int(run["id"]))
    if str(exchange["request_format"]) != EVIDENCE_LEDGER_FORMAT:
        return legacy_semantic_overview(database, recording_id)
    request = _json_object(exchange["request_json"])
    candidates: list[dict[str, Any]] = []
    reviewed = 0
    for row in database.list_semantic_candidates(int(run["id"])):
        revisions = database.list_semantic_candidate_revisions(int(row["id"]))
        latest = revisions[-1] if revisions else None
        if latest is not None:
            reviewed += 1
        evidence = _json_object(row["evidence_json"])
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        review_clips = [
            {
                **clip,
                "audio_url": (
                    "/api/speaker-timeline/audio"
                    f"?recording_id={recording_id}"
                    f"&start_ms={int(clip['start_ms'])}"
                    f"&end_ms={int(clip['end_ms'])}"
                ),
            }
            for clip in evidence.get("review_clips", [])
        ]
        candidates.append(
            {
                "id": int(row["id"]),
                "key": str(row["candidate_key"]),
                "type": str(row["candidate_type"]),
                "semantic_unit": str(evidence.get("semantic_unit") or "event"),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "title": str(latest["title"] if latest else row["title"]),
                "body": str(latest["body"] if latest else row["body"]),
                "base_title": str(row["title"]),
                "base_body": str(row["body"]),
                "review_status": str(latest["status"]) if latest else None,
                "revision_index": (
                    int(latest["revision_index"]) if latest else 0
                ),
                "review_note": latest["note"] if latest else None,
                "evidence": evidence,
                "review_clips": review_clips,
                "audio_url": (
                    review_clips[0]["audio_url"]
                    if len(review_clips) == 1
                    else None
                ),
            }
        )
    summary = _json_object(run["summary_json"])
    ledger = request.get("evidence_ledger") or {}
    transport = request.get("llm_transport") or {}
    excluded = [
        {
            "key": str(item["key"]),
            "start_ms": int(item["start_ms"]),
            "end_ms": int(item["end_ms"]),
            "transcript": str(item["transcript"]),
            "token_count": int(item["token_count"]),
            "informative_char_count": int(item["informative_char_count"]),
            "reason": str(item["exclusion_reason"]),
        }
        for item in ledger.get("excluded_blocks", [])
    ]
    return {
        "available": True,
        "recording_id": recording_id,
        "version": "v2-e.0.1",
        "run": {
            "id": int(run["id"]),
            "status": str(run["status"]),
            "completed_at": run["completed_at"],
            "asr_run_id": int(exchange["asr_run_id"]),
            "diarization_run_id": (
                int(exchange["diarization_run_id"])
                if exchange["diarization_run_id"] is not None
                else None
            ),
            "provider": str(exchange["provider"]),
            "model": str(exchange["model"]),
            "request_sha256": str(exchange["request_sha256"]),
            "response_sha256": str(exchange["response_sha256"]),
        },
        "summary": summary,
        "privacy": {
            "network_access": False,
            "audio_bytes_included": bool(exchange["audio_bytes_included"]),
            "source_paths_included": bool(exchange["source_paths_included"]),
            "provider_receives_source_hashes": False,
            "provider_receives_token_database_ids": False,
        },
        "transport": {
            "strategy": transport.get("strategy"),
            "job_count": len(transport.get("jobs") or []),
            "whole_conversation_first": True,
            "hard_conversation_duration_cap_ms": None,
            "review_clip_ms": request.get("boundary_contract", {}).get(
                "review_clip_ms"
            ),
        },
        "excluded_blocks": excluded,
        "reviewed_candidates": reviewed,
        "candidates": candidates,
    }


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


def _compact_conversation(conversation: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": str(conversation["key"]),
        "start_ms": int(conversation["start_ms"]),
        "end_ms": int(conversation["end_ms"]),
        "complete_conversation": True,
        "transcript": str(conversation["transcript"]),
        "speakers": list(conversation["speakers"]),
        "uncertainty": dict(conversation["uncertainty"]),
        "utterances": [
            {
                "key": str(item["key"]),
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
                "speaker": str(item["speaker"]),
                "speaker_kind": str(item["speaker_kind"]),
                "text": str(item["text"]),
                "uncertainty": dict(item["uncertainty"]),
            }
            for item in conversation["utterances"]
        ],
        "asr_alternatives": list(conversation["asr_alternatives"]),
    }


def _pack_llm_jobs(
    conversations: Sequence[dict[str, Any]],
    *,
    settings: SemanticV2E01Settings,
) -> list[dict[str, Any]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for conversation in conversations:
        conversation_chars = len(_canonical_json(conversation))
        if conversation_chars > settings.max_llm_request_chars:
            if current:
                batches.append(current)
                current = []
                current_chars = 0
            batches.extend(
                [item]
                for item in _split_large_conversation(
                    conversation,
                    settings=settings,
                )
            )
            continue
        if (
            current
            and current_chars + conversation_chars
            > settings.max_llm_request_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(conversation)
        current_chars += conversation_chars
    if current:
        batches.append(current)
    return [
        {
            "format": LLM_REQUEST_FORMAT,
            "job_key": f"semantic-job-{index:04d}",
            "task": "extract_evidence_grounded_daily_semantics",
            "instructions": [
                "Treat conversation boundaries as candidates, not ground truth.",
                "Never invent content absent from utterances.",
                "Every fact and action must cite conversation and utterance keys.",
                "Preserve ASR and speaker uncertainty.",
                "Do not rewrite or replace source ASR evidence.",
            ],
            "conversations": batch,
            "response_contract": {
                "required": [
                    "conversation_interpretations",
                    "daily_summary",
                    "facts",
                    "actions",
                ],
                "evidence_keys_required": True,
                "external_actions_require_human_confirmation": True,
            },
            "privacy": {
                "audio_included": False,
                "source_paths_included": False,
                "source_hashes_included": False,
                "biometric_embeddings_included": False,
                "database_token_ids_included": False,
            },
        }
        for index, batch in enumerate(batches, start=1)
    ]


def _split_large_conversation(
    conversation: dict[str, Any],
    *,
    settings: SemanticV2E01Settings,
) -> list[dict[str, Any]]:
    utterances = list(conversation["utterances"])
    if not utterances:
        return [conversation]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for utterance in utterances:
        length = len(_canonical_json(utterance))
        if current and current_chars + length > settings.max_llm_request_chars:
            chunks.append(current)
            overlap = current[-settings.chunk_overlap_utterances :]
            current = list(overlap)
            current_chars = sum(len(_canonical_json(item)) for item in current)
        current.append(utterance)
        current_chars += length
    if current:
        chunks.append(current)
    values: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        values.append(
            {
                **conversation,
                "start_ms": int(chunk[0]["start_ms"]),
                "end_ms": int(chunk[-1]["end_ms"]),
                "complete_conversation": False,
                "parent_start_ms": int(conversation["start_ms"]),
                "parent_end_ms": int(conversation["end_ms"]),
                "chunk_index": index,
                "chunk_count": len(chunks),
                "transcript": "".join(str(item["text"]) for item in chunk),
                "utterances": chunk,
                "context_policy": "utterance-boundary-with-leading-overlap",
            }
        )
    return values


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


def _validate_response(
    response: dict[str, Any],
    conversations: Sequence[dict[str, Any]],
    *,
    duration_ms: int,
) -> None:
    if str(response.get("format")) != SEMANTIC_RESPONSE_FORMAT:
        raise ValueError("语义响应格式不受支持")
    summary = response.get("summary")
    if not isinstance(summary, dict):
        raise TypeError("语义响应缺少 summary")
    response_conversations = response.get("conversations")
    if not isinstance(response_conversations, list):
        raise TypeError("语义响应缺少 conversations")
    expected_keys = [str(item["key"]) for item in conversations]
    actual_keys = [str(item.get("key")) for item in response_conversations]
    if actual_keys != expected_keys:
        raise ValueError("语义响应没有一一对应完整对话证据")
    expected_start = int(conversations[0]["start_ms"]) if conversations else 0
    expected_end = (
        int(conversations[-1]["end_ms"]) if conversations else duration_ms
    )
    if (
        int(summary.get("start_ms", -1)) != expected_start
        or int(summary.get("end_ms", -1)) != expected_end
        or list(summary.get("evidence_conversation_keys") or [])
        != expected_keys
    ):
        raise ValueError("语义摘要没有完整引用对话证据")
    _validate_semantic_text(summary, label="语义摘要")
    for output, source in zip(
        response_conversations, conversations, strict=True
    ):
        expected_utterances = [
            str(item["key"]) for item in source["utterances"]
        ]
        if (
            int(output.get("start_ms", -1)) != int(source["start_ms"])
            or int(output.get("end_ms", -1)) != int(source["end_ms"])
            or list(output.get("evidence_utterance_keys") or [])
            != expected_utterances
        ):
            raise ValueError(f"完整对话 {source['key']} 的证据引用被改变")
        _validate_semantic_text(output, label=f"完整对话 {source['key']}")
    if response.get("facts") != [] or response.get("actions") != []:
        raise ValueError("V2-E.0.1 mock 不允许生成事实或行动项")


def _validate_provider_privacy(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_PROVIDER_KEYS:
                raise ValueError(f"provider payload 含本地证据字段：{path}.{key}")
            _validate_provider_privacy(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_provider_privacy(item, path=f"{path}[{index}]")
    elif isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"provider payload 含二进制数据：{path}")


def _validate_semantic_text(value: dict[str, Any], *, label: str) -> None:
    title = value.get("title")
    body = value.get("body")
    if not isinstance(title, str) or not title.strip():
        raise TypeError(f"{label}缺少非空 title")
    if not isinstance(body, str) or not body.strip():
        raise TypeError(f"{label}缺少非空 body")
    if len(title) > 500 or len(body) > 100_000:
        raise ValueError(f"{label}内容过长")


def _unique_transport_conversations(
    request: dict[str, Any],
) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    utterance_keys: dict[str, set[str]] = {}
    alternative_keys: dict[str, set[str]] = {}
    for job in request.get("jobs") or []:
        for conversation in job.get("conversations") or []:
            key = str(conversation["key"])
            if key not in by_key:
                value = dict(conversation)
                value["start_ms"] = int(
                    conversation.get("parent_start_ms", conversation["start_ms"])
                )
                value["end_ms"] = int(
                    conversation.get("parent_end_ms", conversation["end_ms"])
                )
                value["complete_conversation"] = True
                value["utterances"] = []
                value["asr_alternatives"] = []
                by_key[key] = value
                utterance_keys[key] = set()
                alternative_keys[key] = set()
            value = by_key[key]
            for utterance in conversation.get("utterances") or []:
                utterance_key = str(utterance["key"])
                if utterance_key in utterance_keys[key]:
                    continue
                utterance_keys[key].add(utterance_key)
                value["utterances"].append(dict(utterance))
            for alternative in conversation.get("asr_alternatives") or []:
                alternative_key = str(alternative["key"])
                if alternative_key in alternative_keys[key]:
                    continue
                alternative_keys[key].add(alternative_key)
                value["asr_alternatives"].append(dict(alternative))
    values = list(by_key.values())
    for value in values:
        value["utterances"].sort(
            key=lambda item: (item["start_ms"], item["key"])
        )
        value["transcript"] = "".join(
            str(item["text"]) for item in value["utterances"]
        )
    return sorted(values, key=lambda item: (item["start_ms"], item["key"]))


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


def _write_manifest(
    run_id: int,
    session_id: int,
    config: dict[str, Any],
    evidence_ledger: dict[str, Any],
    provider_request: dict[str, Any],
    response: dict[str, Any],
    summary: dict[str, Any],
) -> Path:
    directory = OUTPUT_DIR / f"session-{session_id:06d}" / "semantic-v2e0"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run_id:06d}.json"
    payload = {
        "format": "AllDayRecording V2-E.0.1 conversation manifest v1",
        "run_id": run_id,
        "config": config,
        "evidence_ledger": evidence_ledger,
        "provider_request": provider_request,
        "response": response,
        "summary": summary,
        "safety": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_receives_source_hashes": False,
            "provider_receives_database_token_ids": False,
            "asr_overwritten": False,
            "external_actions_written": False,
        },
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[:limit] + "…"


def _format_offset(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds // 1_000)
    hours, remainder = divmod(total_seconds, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
