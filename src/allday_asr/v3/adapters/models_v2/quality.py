from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Protocol

from allday_asr.application.semantic.evidence import (
    ConversationEvidenceSettings,
    build_conversation_evidence,
)
from allday_asr.application.semantic.inputs import semantic_tokens
from allday_asr.domain.hashing import canonical_json
from allday_asr.storage.database import Database
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.ports.processing import (
    ArtifactDependencyOutput,
    SpeakerProjectionOutput,
    StageArtifactOutput,
    StageCancellationRequested,
    StageExecutionContext,
    StageExecutionControl,
    StageExecutionResult,
    UtteranceProjectionOutput,
)


ProgressCallback = Callable[[str, str], None]


class V2WorkflowExecutor(Protocol):
    def execute(
        self, session_id: str, progress: ProgressCallback
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExistingV2QualityWorkflowExecutor:
    """Read the immutable V2-C/D/E evidence produced by the existing workflow."""

    database: Database
    run_workflow: Callable[[str, ProgressCallback], object]

    def execute(
        self, session_id: str, progress: ProgressCallback
    ) -> dict[str, Any]:
        summary_value = self.run_workflow(session_id, progress)
        summary = _summary_dict(summary_value)
        asr_run_id = _positive_int(summary.get("asr_run_id"), "asr_run_id")
        diarization_run_id = _positive_int(
            summary.get("diarization_run_id"), "diarization_run_id"
        )
        tokens = semantic_tokens(self.database, asr_run_id, diarization_run_id)
        turns = [
            {
                "id": int(row["id"]),
                "label": str(row["speaker_label"]),
                "kind": str(row["turn_kind"]),
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "confidence": (
                    float(row["confidence"])
                    if row["confidence"] is not None
                    else None
                ),
                "source_refs": [
                    {
                        "source_object_id": int(source["source_object_id"]),
                        "source_sha256": str(source["source_sha256"]),
                        "source_start_ms": int(source["source_start_ms"]),
                        "source_end_ms": int(source["source_end_ms"]),
                    }
                    for source in self.database.list_diarization_turn_sources(
                        int(row["id"])
                    )
                ],
            }
            for row in self.database.list_diarization_turns(diarization_run_id)
        ]
        disagreements = self.database.list_asr_disagreements(asr_run_id)
        conversations, excluded = build_conversation_evidence(
            tokens,
            disagreements,
            settings=ConversationEvidenceSettings(),
        )
        utterances = sorted(
            [
                dict(utterance)
                for conversation in (*conversations, *excluded)
                for utterance in conversation["utterances"]
            ],
            key=lambda item: (
                int(item["start_ms"]),
                int(item["end_ms"]),
                str(item["key"]),
            ),
        )
        return validate_v2_snapshot(
            {
                "format": "AllDayRecording V2 evidence snapshot v1",
                "summary": summary,
                "tokens": tokens,
                "turns": turns,
                "utterances": utterances,
            }
        )


class QualityWorkflowV2Adapter:
    def __init__(self, executor: V2WorkflowExecutor) -> None:
        self.executor = executor

    def execute(
        self,
        context: StageExecutionContext,
        control: StageExecutionControl,
    ) -> StageExecutionResult:
        stage = context.claim.stage.stage
        if stage in {
            "ingest_verified",
            "backup_admitted",
            "window_plan",
            "speech_gate",
            "mobile_projection",
        }:
            return StageExecutionResult(
                checkpoint={"stage": stage, "status": "verified"},
                log_summary=f"{stage} verified by the V3 orchestrator",
            )
        if stage == "asr_and_alignment":

            def progress(current: str, detail: str) -> None:
                if control.heartbeat({"v2_stage": current, "detail": detail}):
                    raise StageCancellationRequested(
                        "V2 workflow cancelled at a progress checkpoint"
                    )

            snapshot = self.executor.execute(
                context.claim.run.session_id,
                progress,
            )
            payload = canonical_json(snapshot).encode("utf-8")
            return StageExecutionResult(
                checkpoint={
                    "token_count": len(snapshot["tokens"]),
                    "utterance_count": len(snapshot["utterances"]),
                },
                log_summary="V2-C/D/E workflow completed and evidence was frozen",
                artifacts=(
                    StageArtifactOutput(
                        kind="v2_evidence_snapshot",
                        payload=payload,
                        producer="existing-v2-quality-workflow",
                        producer_version="v2-workflow.0",
                        metadata=_snapshot_counts(snapshot),
                        input_refs=(context.claim.run.session_id,),
                    ),
                ),
            )
        snapshot = _prior_snapshot(context)
        if stage == "diarization":
            return StageExecutionResult(
                checkpoint={"turn_count": len(snapshot["turns"])},
                log_summary="V2-D speaker turns validated from immutable snapshot",
            )
        if stage == "utterance_projection":
            speakers = sorted(
                {
                    str(value.get("speaker") or "unassigned")
                    for value in snapshot["utterances"]
                }
            )
            utterances = tuple(
                UtteranceProjectionOutput(
                    ordinal=index,
                    start_ms=int(value["start_ms"]),
                    end_ms=int(value["end_ms"]),
                    text=str(value["text"]),
                    speaker_label=str(value.get("speaker") or "unassigned"),
                    evidence=dict(value["evidence"]),
                )
                for index, value in enumerate(snapshot["utterances"])
            )
            return StageExecutionResult(
                checkpoint={"utterance_count": len(utterances)},
                log_summary="V2 tokens were projected to stable V3 utterances",
                speakers=tuple(SpeakerProjectionOutput(label=value) for value in speakers),
                utterances=utterances,
            )
        if stage == "semantic_evidence_optional":
            summary = dict(snapshot["summary"])
            dependencies = tuple(
                ArtifactDependencyOutput(
                    input_type="utterance",
                    input_id=stable_ulid(
                        "utterance", context.claim.run.run_id, index
                    ),
                    input_revision=1,
                )
                for index, _ in enumerate(snapshot["utterances"])
            )
            return StageExecutionResult(
                checkpoint={"semantic_snapshot": True},
                log_summary="V2-E summary was registered as immutable evidence",
                artifacts=(
                    StageArtifactOutput(
                        kind="v2_semantic_evidence",
                        payload=canonical_json(summary).encode("utf-8"),
                        producer="existing-v2-quality-workflow",
                        producer_version="v2-e.0.2",
                        metadata={"utterance_count": len(snapshot["utterances"])},
                        input_refs=tuple(
                            dependency.input_id for dependency in dependencies
                        ),
                        dependencies=dependencies,
                    ),
                ),
            )
        raise ValueError(f"unsupported V3 processing stage: {stage}")


def validate_v2_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    if value.get("format") != "AllDayRecording V2 evidence snapshot v1":
        raise ValueError("V2 evidence snapshot format is invalid")
    summary = value.get("summary")
    tokens = value.get("tokens")
    turns = value.get("turns")
    utterances = value.get("utterances")
    if (
        not isinstance(summary, dict)
        or not isinstance(tokens, list)
        or not isinstance(turns, list)
        or not isinstance(utterances, list)
    ):
        raise ValueError("V2 evidence snapshot collections are invalid")
    for label, rows in (
        ("token", tokens),
        ("turn", turns),
        ("utterance", utterances),
    ):
        previous_start = -1
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"V2 {label} row is not an object")
            start = row.get("start_ms")
            end = row.get("end_ms")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
                or start < 0
                or end <= start
                or start < previous_start
            ):
                raise ValueError(f"V2 {label} coordinates are invalid")
            previous_start = start
    for token in tokens:
        sources = token.get("source_refs")
        if not isinstance(sources, list) or not sources:
            raise ValueError("V2 token has no immutable source coordinates")
    for label, rows in (
        ("token", tokens),
        ("turn", turns),
        ("utterance", utterances),
    ):
        for row in rows:
            sources = (
                row.get("evidence", {}).get("source_refs")
                if label == "utterance" and isinstance(row.get("evidence"), dict)
                else row.get("source_refs")
            )
            if not isinstance(sources, list) or not sources:
                raise ValueError(f"V2 {label} has no immutable source coordinates")
            for source in sources:
                _validate_source_reference(label, source)
    return {
        "format": str(value["format"]),
        "summary": dict(summary),
        "tokens": [dict(row) for row in tokens],
        "turns": [dict(row) for row in turns],
        "utterances": [dict(row) for row in utterances],
    }


def _prior_snapshot(context: StageExecutionContext) -> dict[str, Any]:
    value = context.prior_artifacts.get("v2_evidence_snapshot")
    if value is None:
        raise RuntimeError("V2 evidence snapshot is not available")
    try:
        payload = json.loads(value[1].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("stored V2 evidence snapshot is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("stored V2 evidence snapshot is not an object")
    return validate_v2_snapshot(payload)


def _snapshot_counts(snapshot: Mapping[str, Any]) -> dict[str, int]:
    return {
        "token_count": len(snapshot["tokens"]),
        "turn_count": len(snapshot["turns"]),
        "utterance_count": len(snapshot["utterances"]),
    }


def _validate_source_reference(label: str, value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"V2 {label} source coordinate is not an object")
    object_id = value.get("source_object_id")
    digest = value.get("source_sha256")
    start = value.get("source_start_ms")
    end = value.get("source_end_ms")
    if (
        not isinstance(object_id, int)
        or isinstance(object_id, bool)
        or object_id < 1
        or not isinstance(digest, str)
        or len(digest) != 64
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
    ):
        raise ValueError(f"V2 {label} source coordinates are invalid")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError(f"V2 {label} source digest is invalid") from exc


def _summary_dict(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise ValueError("V2 workflow returned an unsupported summary")


def _positive_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"V2 workflow summary has invalid {label}")
    return value


__all__ = [
    "ExistingV2QualityWorkflowExecutor",
    "QualityWorkflowV2Adapter",
    "V2WorkflowExecutor",
    "validate_v2_snapshot",
]
