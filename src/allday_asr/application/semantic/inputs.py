from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from allday_asr.storage.database import Database


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


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
