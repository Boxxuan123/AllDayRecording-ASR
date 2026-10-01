from __future__ import annotations
import json
from collections.abc import Mapping, Sequence
from typing import Any
from allday_asr.v3.ports.processing import (
    StageExecutionContext,
)

from .native_contracts import (
    _Source,
)
from .native_runtime import _json_safe
from allday_asr.v3.domain.speaker_turns import (
    PROJECTION_VERSION, foreign_turns, can_merge_same_speaker_tokens,
)


def _artifact_json(context: StageExecutionContext, kind: str) -> dict[str, Any]:
    value = context.prior_artifacts.get(kind)
    if value is None:
        raise RuntimeError(f"V3 prior artifact is unavailable: {kind}")
    try:
        payload = json.loads(value[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"V3 artifact is invalid JSON: {kind}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"V3 artifact is not an object: {kind}")
    return payload


def _backend_manifest(backend: Any) -> dict[str, Any]:
    return {
        "backend": str(backend.backend_name),
        "model_id": str(backend.model_id),
        "model_revision": backend.model_revision,
        "parameters": _json_safe(backend.parameters()),
    }


def _turn(value: Any, duration_ms: int) -> dict[str, Any] | None:
    start = max(0, min(duration_ms, int(value.start_ms)))
    end = max(0, min(duration_ms, int(value.end_ms)))
    if end <= start:
        return None
    return {
        "start_ms": start,
        "end_ms": end,
        "speaker_label": str(value.speaker_label),
        "confidence": value.confidence,
        "metadata": dict(value.metadata or {}),
    }


def _speaker_for(
    token: Mapping[str, Any], turns: Sequence[Mapping[str, Any]], threshold: float
) -> str | None:
    start, end = int(token["start_ms"]), int(token["end_ms"])
    duration = max(1, end - start)
    scores: dict[str, int] = {}
    for turn in turns:
        overlap = max(
            0, min(end, int(turn["end_ms"])) - max(start, int(turn["start_ms"]))
        )
        if overlap:
            label = str(turn["speaker_label"])
            scores[label] = scores.get(label, 0) + overlap
    if not scores:
        return None
    label, overlap = max(scores.items(), key=lambda item: (item[1], item[0]))
    return label if overlap / duration >= threshold else None


def _group_utterances_legacy(tokens: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for token in sorted(
        tokens, key=lambda item: (int(item["start_ms"]), int(item["end_ms"]))
    ):
        text = str(token["text"])
        if not text:
            continue
        speaker = token.get("speaker")
        if (
            not groups
            or groups[-1]["speaker"] != speaker
            or int(token["start_ms"]) - int(groups[-1]["end_ms"]) > 1_200
            or int(token["end_ms"]) - int(groups[-1]["start_ms"]) > 30_000
        ):
            groups.append(
                {
                    "start_ms": int(token["start_ms"]),
                    "end_ms": int(token["end_ms"]),
                    "text": text,
                    "speaker": speaker,
                    "token_count": 1,
                    "source_refs": list(token["source_refs"]),
                }
            )
            continue
        current = groups[-1]
        current["end_ms"] = int(token["end_ms"])
        current["text"] = _join_text(str(current["text"]), text)
        current["token_count"] = int(current["token_count"]) + 1
        known = {
            (value["asset_id"], value["source_start_ms"], value["source_end_ms"])
            for value in current["source_refs"]
        }
        current["source_refs"].extend(
            value
            for value in token["source_refs"]
            if (value["asset_id"], value["source_start_ms"], value["source_end_ms"])
            not in known
        )
    return groups


def _group_utterances(tokens, exclusive_turns=()):
    """Retain each text token once; isolate tokens crossing exclusive speakers.

    A crossing token keeps its majority attribution in provenance but is an
    unassigned singleton in the transcript: its text/timestamps are never split,
    copied, clipped or thrown away to fabricate a clean speaker utterance.
    """
    groups = []
    for original in sorted(tokens, key=lambda t: (t["start_ms"], t["end_ms"])):
        if not str(original["text"]):
            continue
        token = dict(original)
        attributed = token.get("speaker")
        foreign = foreign_turns(token["start_ms"], token["end_ms"], attributed,
                                exclusive_turns)
        token["speaker_boundary_crossing"] = bool(foreign)
        if foreign:
            token["speaker"] = None
        provenance = {"start_ms": token["start_ms"], "end_ms": token["end_ms"],
                      "attributed_speaker": attributed,
                      "foreign_exclusive_turns": foreign,
                      "source_refs": list(token["source_refs"])}
        if not groups or not can_merge_same_speaker_tokens(groups[-1], token, exclusive_turns):
            groups.append({"start_ms": token["start_ms"], "end_ms": token["end_ms"],
                           "text": str(token["text"]), "speaker": token.get("speaker"),
                           "token_count": 1, "source_refs": list(token["source_refs"]),
                           "speaker_boundary_crossing": bool(foreign),
                           "token_attributions": [provenance],
                           "projection_version": PROJECTION_VERSION})
            continue
        current = groups[-1]
        current["end_ms"] = max(current["end_ms"], token["end_ms"])
        current["text"] = _join_text(current["text"], str(token["text"]))
        current["token_count"] += 1
        current["token_attributions"].append(provenance)
        known = {(r["asset_id"], r["source_start_ms"], r["source_end_ms"])
                 for r in current["source_refs"]}
        current["source_refs"].extend(r for r in token["source_refs"]
            if (r["asset_id"], r["source_start_ms"], r["source_end_ms"]) not in known)
    return groups


def _join_text(left: str, right: str) -> str:
    if not left or not right:
        return left + right
    if (
        left[-1].isascii()
        and right[0].isascii()
        and left[-1].isalnum()
        and right[0].isalnum()
    ):
        return f"{left} {right}"
    return left + right


def _source_refs(
    sources: Sequence[_Source], start_ms: int, end_ms: int
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for source in sources:
        overlap_start = max(start_ms, source.session_start_ms)
        overlap_end = min(end_ms, source.session_end_ms)
        if overlap_end <= overlap_start:
            continue
        asset_start = source.source_start_ms + overlap_start - source.session_start_ms
        values.append(
            {
                "asset_id": source.asset_id,
                "sha256": source.sha256,
                "session_start_ms": overlap_start,
                "session_end_ms": overlap_end,
                "source_start_ms": asset_start,
                "source_end_ms": asset_start + overlap_end - overlap_start,
            }
        )
    return values
