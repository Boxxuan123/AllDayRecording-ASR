from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from allday_asr.paths import OUTPUT_DIR
from allday_asr.storage.database import Database

SEMANTIC_REQUEST_FORMAT = "AllDayRecording semantic evidence request v1"
SEMANTIC_RESPONSE_FORMAT = "AllDayRecording semantic candidate response v1"


@dataclass(frozen=True)
class SemanticV2E0Settings:
    event_gap_ms: int = 45_000
    max_event_ms: int = 120_000
    transcript_preview_chars: int = 280

    def validate(self) -> None:
        if self.event_gap_ms < 1_000:
            raise ValueError("event_gap_ms 不能小于 1000")
        if not 10_000 <= self.max_event_ms <= 120_000:
            raise ValueError("max_event_ms 必须在 10 秒到 120 秒之间")
        if not 80 <= self.transcript_preview_chars <= 2_000:
            raise ValueError("transcript_preview_chars 必须在 80 到 2000 之间")


@dataclass(frozen=True)
class SemanticV2E0Summary:
    run_id: int
    asr_run_id: int
    diarization_run_id: int | None
    event_count: int
    token_count: int
    candidate_count: int
    request_sha256: str
    response_sha256: str
    manifest_path: Path


class SemanticProvider(Protocol):
    provider_name: str
    model_name: str
    network_access: bool

    def generate(self, request: dict[str, Any]) -> dict[str, Any]: ...


class DeterministicMockSemanticProvider:
    """Local record/replay provider that intentionally performs no semantics."""

    provider_name = "local_mock"
    model_name = "evidence-packager-v1"
    network_access = False

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        events = list(request.get("events") or [])
        if not events:
            raise ValueError("语义请求没有事件")
        first_start = int(events[0]["start_ms"])
        last_end = int(events[-1]["end_ms"])
        return {
            "format": SEMANTIC_RESPONSE_FORMAT,
            "mode": "local_mock_evidence_only",
            "summary": {
                "key": "daily-summary",
                "start_ms": first_start,
                "end_ms": last_end,
                "title": "V2-E.0 本地语义证据包",
                "body": (
                    f"已整理 {len(events)} 个带原音坐标的事件。"
                    "当前是本地 mock，没有生成自然语言日记，也没有调用云服务。"
                ),
                "evidence_event_keys": [str(item["key"]) for item in events],
            },
            "events": [
                {
                    "key": str(event["key"]),
                    "start_ms": int(event["start_ms"]),
                    "end_ms": int(event["end_ms"]),
                    "title": (
                        f"证据事件 {index:02d} · "
                        f"{_format_offset(int(event['start_ms']))}"
                    ),
                    "body": (
                        "待云端语义模型处理。对齐文字预览："
                        + str(event["transcript_preview"])
                    ),
                    "evidence_token_ids": list(event["evidence"]["token_ids"]),
                    "source_refs": list(event["evidence"]["source_refs"]),
                    "uncertainty": dict(event["uncertainty"]),
                }
                for index, event in enumerate(events, start=1)
            ],
            "facts": [],
            "actions": [],
            "limitations": [
                "No cloud or local LLM was called.",
                "Event titles are deterministic time labels, not semantic claims.",
                "Facts and actions intentionally remain empty in V2-E.0 mock mode.",
            ],
        }


def run_semantic_v2e0(
    database: Database,
    recording_id: int,
    *,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
    settings: SemanticV2E0Settings | None = None,
    provider: SemanticProvider | None = None,
) -> SemanticV2E0Summary:
    """Build an immutable local semantic exchange without any cloud request."""
    settings = settings or SemanticV2E0Settings()
    settings.validate()
    provider = provider or DeterministicMockSemanticProvider()
    if provider.network_access:
        raise ValueError("V2-E.0 只允许不联网 provider")
    asr_run, diarization_run = resolve_semantic_input_runs(
        database,
        recording_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
    )
    session = database.get_session_for_recording(recording_id)
    tokens = semantic_tokens(
        database,
        int(asr_run["id"]),
        int(diarization_run["id"]) if diarization_run is not None else None,
    )
    if not tokens:
        raise RuntimeError("V2-E.0 没有可用的 committed ASR token")
    disagreement_rows = database.list_asr_disagreements(int(asr_run["id"]))
    events = build_semantic_events(
        tokens,
        disagreement_rows,
        settings=settings,
    )
    request = build_semantic_request(
        database,
        recording_id,
        session_id=int(session["id"]),
        asr_run_id=int(asr_run["id"]),
        diarization_run_id=(
            int(diarization_run["id"])
            if diarization_run is not None
            else None
        ),
        events=events,
    )
    request_sha256 = _sha256_json(request)
    config = {
        "request_format": SEMANTIC_REQUEST_FORMAT,
        "response_format": SEMANTIC_RESPONSE_FORMAT,
        "asr_run_id": int(asr_run["id"]),
        "diarization_run_id": (
            int(diarization_run["id"])
            if diarization_run is not None
            else None
        ),
        "provider": provider.provider_name,
        "model": provider.model_name,
        "event_gap_ms": settings.event_gap_ms,
        "max_event_ms": settings.max_event_ms,
        "transcript_preview_chars": settings.transcript_preview_chars,
        "request_sha256": request_sha256,
        "privacy": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
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
        },
        pipeline_version="v2-e.0",
        parent_run_id=(
            int(diarization_run["id"])
            if diarization_run is not None
            else int(asr_run["id"])
        ),
    )
    try:
        response = provider.generate(request)
        _validate_response(response, events)
        response_sha256 = _sha256_json(response)
        candidates = response_candidates(run_id, response, events)
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
                "request_format": SEMANTIC_REQUEST_FORMAT,
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
        summary_payload = {
            "session_id": int(session["id"]),
            "asr_run_id": int(asr_run["id"]),
            "diarization_run_id": (
                int(diarization_run["id"])
                if diarization_run is not None
                else None
            ),
            "provider": provider.provider_name,
            "model": provider.model_name,
            "mode": "local_mock_evidence_only",
            "event_count": len(events),
            "token_count": len(tokens),
            "candidate_count": len(candidates),
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "facts": 0,
            "actions": 0,
            "privacy": {
                "network_access": False,
                "audio_bytes_included": False,
                "source_paths_included": False,
                "biometric_data_included": False,
            },
        }
        manifest_path = _write_manifest(
            run_id,
            int(session["id"]),
            config,
            request,
            response,
            summary_payload,
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        return SemanticV2E0Summary(
            run_id=run_id,
            asr_run_id=int(asr_run["id"]),
            diarization_run_id=(
                int(diarization_run["id"])
                if diarization_run is not None
                else None
            ),
            event_count=len(events),
            token_count=len(tokens),
            candidate_count=len(candidates),
            request_sha256=request_sha256,
            response_sha256=response_sha256,
            manifest_path=manifest_path,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def resolve_semantic_input_runs(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
) -> tuple[Any, Any | None]:
    if session_id is None:
        if recording_id is None:
            raise ValueError("语义阶段必须指定 recording_id 或 session_id")
        session_id = int(database.get_session_for_recording(recording_id)["id"])
    else:
        session = database.get_recording_session(session_id)
        if (
            recording_id is not None
            and session["legacy_recording_id"] is not None
            and int(session["legacy_recording_id"]) != recording_id
        ):
            raise ValueError("recording_id 与 session_id 不属于同一会话")
    runs = database.list_session_processing_runs(session_id)
    if diarization_run_id is None:
        diarization_candidates = [
            row
            for row in runs
            if str(row["run_kind"]) == "quality_diarization_v2d"
            and str(row["status"]) == "completed"
        ]
        diarization_run = (
            diarization_candidates[-1] if diarization_candidates else None
        )
    else:
        diarization_run = database.get_processing_run(diarization_run_id)
        if int(diarization_run["session_id"]) != session_id:
            raise ValueError("V2-D run 不属于当前录音会话")
        if (
            str(diarization_run["run_kind"]) != "quality_diarization_v2d"
            or str(diarization_run["status"]) != "completed"
        ):
            raise ValueError("V2-E.0 需要已完成的 V2-D run")

    inferred_asr_run_id: int | None = None
    if diarization_run is not None:
        diarization_summary = _json_object(diarization_run["summary_json"])
        inferred_asr_run_id = int(
            diarization_summary.get("asr_run_id")
            or diarization_run["parent_run_id"]
            or 0
        ) or None
    if asr_run_id is None:
        if inferred_asr_run_id is not None:
            asr_run_id = inferred_asr_run_id
        else:
            asr_candidates = [
                row
                for row in runs
                if str(row["run_kind"]) == "quality_asr_v2c"
                and str(row["status"]) == "completed"
            ]
            if not asr_candidates:
                raise RuntimeError("当前录音没有已完成的 V2-C ASR run")
            asr_run_id = int(asr_candidates[-1]["id"])
    asr_run = database.get_processing_run(asr_run_id)
    if int(asr_run["session_id"]) != session_id:
        raise ValueError("V2-C run 不属于当前录音会话")
    if (
        str(asr_run["run_kind"]) != "quality_asr_v2c"
        or str(asr_run["status"]) != "completed"
    ):
        raise ValueError("V2-E.0 需要已完成的 V2-C ASR run")
    if inferred_asr_run_id is not None and inferred_asr_run_id != int(asr_run["id"]):
        raise ValueError("V2-D run 的 token 归属与所选 V2-C run 不一致")
    return asr_run, diarization_run


def semantic_tokens(
    database: Database,
    asr_run_id: int,
    diarization_run_id: int | None,
) -> list[dict[str, Any]]:
    token_rows = database.list_committed_asr_tokens(asr_run_id)
    source_rows = database.list_asr_token_sources_for_run(asr_run_id)
    sources_by_token: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        sources_by_token[int(row["token_id"])].append(
            {
                "source_object_id": int(row["source_object_id"]),
                "source_instance_id": (
                    int(row["source_instance_id"])
                    if row["source_instance_id"] is not None
                    else None
                ),
                "source_sha256": str(row["source_sha256"]),
                "source_start_ms": int(row["source_start_ms"]),
                "source_end_ms": int(row["source_end_ms"]),
            }
        )
    attributions_by_token: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if diarization_run_id is not None:
        for row in database.list_token_speaker_attributions(diarization_run_id):
            attributions_by_token[int(row["token_id"])].append(
                {
                    "speaker": row["speaker_label"],
                    "kind": str(row["attribution_kind"]),
                    "rank": int(row["rank"]),
                    "confidence": (
                        float(row["confidence"])
                        if row["confidence"] is not None
                        else None
                    ),
                }
            )
    tokens: list[dict[str, Any]] = []
    for row in token_rows:
        token_id = int(row["id"])
        source_refs = sources_by_token.get(token_id, [])
        if not source_refs:
            raise RuntimeError(f"committed token {token_id} 没有不可变原音坐标")
        attributions = sorted(
            attributions_by_token.get(token_id, []),
            key=lambda item: int(item["rank"]),
        )
        primary = attributions[0] if attributions else None
        tokens.append(
            {
                "id": token_id,
                "text": str(row["text"]),
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "speaker": primary["speaker"] if primary else None,
                "speaker_kind": primary["kind"] if primary else "none",
                "speaker_confidence": primary["confidence"] if primary else None,
                "has_overlap": any(
                    item["kind"] == "overlap" for item in attributions
                ),
                "source_refs": source_refs,
            }
        )
    return tokens


def build_semantic_events(
    tokens: Sequence[dict[str, Any]],
    disagreement_rows: Sequence[Any],
    *,
    settings: SemanticV2E0Settings,
) -> list[dict[str, Any]]:
    if not tokens:
        return []
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for token in sorted(tokens, key=lambda item: (item["start_ms"], item["id"])):
        if current:
            gap_ms = int(token["start_ms"]) - int(current[-1]["end_ms"])
            projected_span = int(token["end_ms"]) - int(current[0]["start_ms"])
            if gap_ms > settings.event_gap_ms or projected_span > settings.max_event_ms:
                groups.append(current)
                current = []
        current.append(dict(token))
    if current:
        groups.append(current)

    events: list[dict[str, Any]] = []
    for index, group in enumerate(groups, start=1):
        start_ms = int(group[0]["start_ms"])
        end_ms = int(group[-1]["end_ms"])
        disagreement_ids = [
            int(row["id"])
            for row in disagreement_rows
            if int(row["session_end_ms"]) > start_ms
            and int(row["session_start_ms"]) < end_ms
        ]
        speaker_counts = Counter(
            str(token["speaker"])
            for token in group
            if token.get("speaker")
        )
        transcript = "".join(str(token["text"]) for token in group).strip()
        source_refs = _deduplicate_source_refs(
            source_ref
            for token in group
            for source_ref in token["source_refs"]
        )
        events.append(
            {
                "key": f"event-{index:04d}",
                "start_ms": start_ms,
                "end_ms": end_ms,
                "transcript": transcript,
                "transcript_preview": _truncate(
                    transcript, settings.transcript_preview_chars
                ),
                "token_count": len(group),
                "speakers": [
                    {"label": label, "token_count": count}
                    for label, count in sorted(
                        speaker_counts.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ],
                "uncertainty": {
                    "unassigned_tokens": sum(
                        token["speaker_kind"] == "none" for token in group
                    ),
                    "uncertain_speaker_tokens": sum(
                        token["speaker_kind"] == "uncertain" for token in group
                    ),
                    "overlap_tokens": sum(
                        bool(token["has_overlap"]) for token in group
                    ),
                    "disagreement_ids": disagreement_ids,
                },
                "evidence": {
                    "token_ids": [int(token["id"]) for token in group],
                    "source_refs": source_refs,
                },
                "tokens": group,
            }
        )
    return events


def build_semantic_request(
    database: Database,
    recording_id: int,
    *,
    session_id: int,
    asr_run_id: int,
    diarization_run_id: int | None,
    events: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    recording = database.get_recording(recording_id)
    session = database.get_recording_session(session_id)
    return {
        "format": SEMANTIC_REQUEST_FORMAT,
        "mode": "local_record_only",
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
        "privacy": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "raw_audio_identifiers": "sha256-and-time-coordinates-only",
        },
        "output_contract": {
            "required": ["summary", "events", "facts", "actions"],
            "evidence_required_for_every_claim": True,
            "may_overwrite_asr": False,
            "external_write_requires_human_confirmation": True,
        },
        "events": list(events),
    }


def response_candidates(
    run_id: int,
    response: dict[str, Any],
    events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    events_by_key = {str(item["key"]): item for item in events}
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
                "event_keys": list(summary["evidence_event_keys"]),
                "mock_only": True,
            },
        }
    ]
    for item in response["events"]:
        source_event = events_by_key[str(item["key"])]
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
                    "event_key": str(item["key"]),
                    "token_ids": list(item["evidence_token_ids"]),
                    "source_refs": list(item["source_refs"]),
                    "uncertainty": dict(item["uncertainty"]),
                    "speaker_counts": list(source_event["speakers"]),
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
        can_generate = True
        reason = "还没有 V2-E.0 本地语义证据包。"
        try:
            asr_run, diarization_run = resolve_semantic_input_runs(
                database, recording_id
            )
            inputs = {
                "asr_run_id": int(asr_run["id"]),
                "diarization_run_id": (
                    int(diarization_run["id"])
                    if diarization_run is not None
                    else None
                ),
            }
        except (KeyError, RuntimeError, ValueError) as exc:
            can_generate = False
            reason = str(exc)
            inputs = None
        return {
            "available": False,
            "recording_id": recording_id,
            "can_generate": can_generate,
            "reason": reason,
            "inputs": inputs,
        }
    run = completed[-1]
    exchange = database.get_semantic_exchange(int(run["id"]))
    candidates = []
    reviewed = 0
    for row in database.list_semantic_candidates(int(run["id"])):
        revisions = database.list_semantic_candidate_revisions(int(row["id"]))
        latest = revisions[-1] if revisions else None
        if latest is not None:
            reviewed += 1
        evidence = _json_object(row["evidence_json"])
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        candidates.append(
            {
                "id": int(row["id"]),
                "key": str(row["candidate_key"]),
                "type": str(row["candidate_type"]),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "title": str(latest["title"] if latest else row["title"]),
                "body": str(latest["body"] if latest else row["body"]),
                "base_title": str(row["title"]),
                "base_body": str(row["body"]),
                "review_status": str(latest["status"]) if latest else None,
                "revision_index": int(latest["revision_index"]) if latest else 0,
                "review_note": latest["note"] if latest else None,
                "evidence": evidence,
                "audio_url": (
                    f"/api/speaker-timeline/audio?recording_id={recording_id}"
                    f"&start_ms={start_ms}&end_ms={end_ms}"
                    if str(row["candidate_type"]) == "event"
                    and end_ms - start_ms <= 120_000
                    else None
                ),
            }
        )
    summary = _json_object(run["summary_json"])
    return {
        "available": True,
        "recording_id": recording_id,
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
        },
        "reviewed_candidates": reviewed,
        "candidates": candidates,
    }


def review_semantic_candidate(
    database: Database,
    recording_id: int,
    candidate_id: int,
    *,
    status: str,
    title: str | None = None,
    body: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    candidate = database.get_semantic_candidate(candidate_id)
    run = database.get_processing_run(int(candidate["run_id"]))
    if int(run["recording_id"]) != recording_id:
        raise ValueError("语义候选不属于当前录音")
    revision = database.create_semantic_candidate_revision(
        candidate_id,
        status=status,
        title=title,
        body=body,
        note=note,
    )
    return {
        "candidate_id": candidate_id,
        "run_id": int(candidate["run_id"]),
        "status": str(revision["status"]),
        "title": str(revision["title"]),
        "body": str(revision["body"]),
        "note": revision["note"],
        "revision_index": int(revision["revision_index"]),
        "created_at": str(revision["created_at"]),
    }


def _validate_response(
    response: dict[str, Any], events: Sequence[dict[str, Any]]
) -> None:
    if str(response.get("format")) != SEMANTIC_RESPONSE_FORMAT:
        raise ValueError("语义响应格式不受支持")
    if not isinstance(response.get("summary"), dict):
        raise TypeError("语义响应缺少 summary")
    response_events = response.get("events")
    if not isinstance(response_events, list):
        raise TypeError("语义响应缺少 events")
    expected = [str(item["key"]) for item in events]
    actual = [str(item.get("key")) for item in response_events]
    if actual != expected:
        raise ValueError("语义响应事件没有一一对应输入证据")
    summary = response["summary"]
    if (
        int(summary.get("start_ms", -1)) != int(events[0]["start_ms"])
        or int(summary.get("end_ms", -1)) != int(events[-1]["end_ms"])
        or list(summary.get("evidence_event_keys") or []) != expected
    ):
        raise ValueError("语义摘要没有完整引用输入事件")
    _validate_semantic_text(summary, label="语义摘要")
    for response_event, source_event in zip(
        response_events, events, strict=True
    ):
        if (
            int(response_event.get("start_ms", -1))
            != int(source_event["start_ms"])
            or int(response_event.get("end_ms", -1))
            != int(source_event["end_ms"])
            or list(response_event.get("evidence_token_ids") or [])
            != list(source_event["evidence"]["token_ids"])
            or list(response_event.get("source_refs") or [])
            != list(source_event["evidence"]["source_refs"])
            or dict(response_event.get("uncertainty") or {})
            != dict(source_event["uncertainty"])
        ):
            raise ValueError(
                f"语义事件 {source_event['key']} 的原音证据被改变"
            )
        _validate_semantic_text(
            response_event, label=f"语义事件 {source_event['key']}"
        )
    if response.get("facts") != [] or response.get("actions") != []:
        raise ValueError("V2-E.0 mock 不允许生成事实或行动项")


def _validate_semantic_text(value: dict[str, Any], *, label: str) -> None:
    title = value.get("title")
    body = value.get("body")
    if not isinstance(title, str) or not title.strip():
        raise TypeError(f"{label}缺少非空 title")
    if not isinstance(body, str) or not body.strip():
        raise TypeError(f"{label}缺少非空 body")
    if len(title) > 500 or len(body) > 100_000:
        raise ValueError(f"{label}内容过长")


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
    request: dict[str, Any],
    response: dict[str, Any],
    summary: dict[str, Any],
) -> Path:
    directory = OUTPUT_DIR / f"session-{session_id:06d}" / "semantic-v2e0"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"run-{run_id:06d}.json"
    payload = {
        "format": "AllDayRecording V2-E.0 local semantic exchange manifest v1",
        "run_id": run_id,
        "config": config,
        "request": request,
        "response": response,
        "summary": summary,
        "safety": {
            "network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
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


def _sha256_json(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
