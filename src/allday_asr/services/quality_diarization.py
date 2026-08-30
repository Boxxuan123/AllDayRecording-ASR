from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from allday_asr.diarization.quality_backends import (
    QualityDiarizationBackend,
    SpeakerTurn,
)
from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_mapping
from allday_asr.paths import OUTPUT_DIR
from allday_asr.services.sources import (
    LogicalWindow,
    resolve_session_slices,
    temporary_logical_window,
)
from allday_asr.storage.database import Database


@dataclass(frozen=True)
class QualityDiarizationSettings:
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    min_primary_overlap_ratio: float = 0.50
    min_secondary_overlap_ratio: float = 0.30
    min_primary_margin: float = 0.15
    model_signature: str = ""
    pipeline_revision: str = "v2d-overlap-speaker-timeline-v1"

    def __post_init__(self) -> None:
        for name in ("num_speakers", "min_speakers", "max_speakers"):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive")
        if self.num_speakers is None and (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers cannot exceed max_speakers")
        for name in (
            "min_primary_overlap_ratio",
            "min_secondary_overlap_ratio",
            "min_primary_margin",
        ):
            if not 0 <= getattr(self, name) <= 1:
                raise ValueError(f"{name} must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def sha256(self) -> str:
        return _sha256_mapping(self.to_dict())


@dataclass(frozen=True)
class QualityDiarizationSummary:
    run_id: int
    session_id: int
    asr_run_id: int
    regular_turns: int
    exclusive_turns: int
    speakers: int
    overlap_regions: int
    overlap_ms: int
    attributed_tokens: int
    primary_tokens: int
    overlap_tokens: int
    uncertain_tokens: int
    unassigned_tokens: int
    manifest_path: Path


@dataclass(frozen=True)
class QualityDiarizationSnapshotSummary:
    prediction_set_id: int
    prediction_count: int
    speaker_predictions: int
    overlap_predictions: int
    content_sha256: str


BackendFactory = Callable[[], QualityDiarizationBackend]


def run_quality_diarization(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    asr_run_id: int,
    settings: QualityDiarizationSettings,
    backend_factory: BackendFactory,
) -> QualityDiarizationSummary:
    if session_id is None:
        if recording_id is None:
            raise ValueError("V2-D 必须指定 recording_id 或 session_id")
        session = database.get_session_for_recording(recording_id)
        session_id = int(session["id"])
    else:
        session = database.get_recording_session(session_id)
        if (
            recording_id is not None
            and session["legacy_recording_id"] is not None
            and int(session["legacy_recording_id"]) != recording_id
        ):
            raise ValueError("recording_id 与 session_id 不属于同一会话")
    duration_ms = int(session["duration_ms"])
    asr_run = database.get_processing_run(asr_run_id)
    if str(asr_run["run_kind"]) != "quality_asr_v2c":
        raise ValueError("V2-D requires a V2-C ASR run")
    if str(asr_run["status"]) != "completed":
        raise ValueError("V2-D requires a completed V2-C ASR run")
    if int(asr_run["session_id"]) != session_id:
        raise ValueError("V2-D and V2-C runs belong to different sessions")

    slices, gaps = resolve_session_slices(database, session_id, 0, duration_ms)
    if gaps:
        raise ValueError(f"录音会话存在未覆盖的原始音频范围：{gaps}")
    window = LogicalWindow(
        session_id=session_id,
        index=0,
        core_start_ms=0,
        core_end_ms=duration_ms,
        analysis_start_ms=0,
        analysis_end_ms=duration_ms,
        slices=slices,
        uncovered_ranges=gaps,
    )

    backend = backend_factory()
    try:
        # Loading first ensures a gated-model authorization problem cannot leave a
        # misleading partial processing run.
        backend.ensure_loaded()
        model_manifest = _backend_manifest(backend)
        run_config = {**settings.to_dict(), "asr_run_id": asr_run_id}
        run_id = database.start_processing_run(
            recording_id,
            session_id=session_id,
            run_kind="quality_diarization_v2d",
            config=run_config,
            config_sha256=_sha256_mapping(run_config),
            model_manifest=model_manifest,
            pipeline_version="v2-d",
            parent_run_id=asr_run_id,
        )
        try:
            with temporary_logical_window(window) as audio_path:
                result = backend.diarize(
                    audio_path,
                    num_speakers=settings.num_speakers,
                    min_speakers=settings.min_speakers,
                    max_speakers=settings.max_speakers,
                )
            regular = _clip_turns(result.regular_turns, duration_ms)
            exclusive = _clip_turns(result.exclusive_turns, duration_ms)
            turn_values = _trace_turns(
                database,
                run_id,
                session_id,
                regular,
                exclusive,
            )
            database.create_diarization_turns(run_id, session_id, turn_values)

            tokens = database.list_committed_asr_tokens(asr_run_id)
            attributions = attribute_tokens_to_speakers(
                tokens,
                regular,
                exclusive,
                run_id=run_id,
                settings=settings,
            )
            database.create_token_speaker_attributions(
                run_id, asr_run_id, attributions
            )
            overlap_regions = compute_overlap_regions(regular)
            token_kinds: dict[int, set[str]] = defaultdict(set)
            for row in attributions:
                token_kinds[int(row["token_id"])].add(str(row["attribution_kind"]))
            summary_payload = {
                "session_id": session_id,
                "asr_run_id": asr_run_id,
                "regular_turns": len(regular),
                "exclusive_turns": len(exclusive),
                "speakers": len({turn.speaker_label for turn in regular}),
                "overlap_regions": len(overlap_regions),
                "overlap_ms": sum(end - start for start, end, _ in overlap_regions),
                "attributed_tokens": len(tokens),
                "primary_tokens": sum(
                    "primary" in kinds for kinds in token_kinds.values()
                ),
                "overlap_tokens": sum(
                    "overlap" in kinds for kinds in token_kinds.values()
                ),
                "uncertain_tokens": sum(
                    "uncertain" in kinds for kinds in token_kinds.values()
                ),
                "unassigned_tokens": sum("none" in kinds for kinds in token_kinds.values()),
            }
            manifest_path = _write_manifest(
                database,
                run_id,
                settings,
                result.raw_response,
                summary_payload,
            )
            database.finish_processing_run(
                run_id,
                status="completed",
                summary=summary_payload,
                artifacts={"manifest": str(manifest_path)},
            )
            return QualityDiarizationSummary(
                run_id=run_id,
                manifest_path=manifest_path,
                **summary_payload,
            )
        except Exception as exc:
            database.finish_processing_run(run_id, status="failed", error=repr(exc))
            raise
    finally:
        backend.close()


def attribute_tokens_to_speakers(
    tokens: Sequence[Any],
    regular_turns: Sequence[SpeakerTurn],
    exclusive_turns: Sequence[SpeakerTurn],
    *,
    run_id: int,
    settings: QualityDiarizationSettings,
) -> list[dict[str, Any]]:
    """Fuse exclusive primary labels with true concurrent-speaker evidence."""
    overlap_regions = compute_overlap_regions(regular_turns)
    rows: list[dict[str, Any]] = []
    for token in tokens:
        token_id = int(token["id"])
        start_ms = int(token["session_start_ms"])
        end_ms = int(token["session_end_ms"])
        duration_ms = end_ms - start_ms
        exclusive_scores = _speaker_overlap_scores(
            start_ms, end_ms, exclusive_turns
        )
        regular_scores = _speaker_overlap_scores(start_ms, end_ms, regular_turns)
        concurrent_scores = _concurrent_speaker_scores(
            start_ms, end_ms, overlap_regions
        )
        ranked_exclusive = sorted(
            exclusive_scores.items(), key=lambda item: (-item[1], item[0])
        )
        best_label = ranked_exclusive[0][0] if ranked_exclusive else None
        best_ms = ranked_exclusive[0][1] if ranked_exclusive else 0
        second_ms = ranked_exclusive[1][1] if len(ranked_exclusive) > 1 else 0
        best_ratio = best_ms / duration_ms
        margin = (best_ms - second_ms) / duration_ms
        primary_label = (
            best_label
            if best_label is not None
            and best_ratio >= settings.min_primary_overlap_ratio
            and margin >= settings.min_primary_margin
            else None
        )
        concurrent_labels = [
            label
            for label, overlap_ms in sorted(
                concurrent_scores.items(), key=lambda item: (-item[1], item[0])
            )
            if overlap_ms / duration_ms >= settings.min_secondary_overlap_ratio
        ]
        metadata = {
            "policy": "exclusive-primary-plus-concurrent-regular-v1",
            "exclusive_overlap_ms": exclusive_scores,
            "regular_overlap_ms": regular_scores,
            "concurrent_overlap_ms": concurrent_scores,
            "primary_margin": margin,
            "thresholds": {
                "primary_overlap_ratio": settings.min_primary_overlap_ratio,
                "secondary_overlap_ratio": settings.min_secondary_overlap_ratio,
                "primary_margin": settings.min_primary_margin,
            },
        }
        decisions: list[tuple[str | None, str, int, float]] = []
        if primary_label is not None:
            decisions.append((primary_label, "primary", best_ms, best_ratio))
            decisions.extend(
                (
                    label,
                    "overlap",
                    concurrent_scores[label],
                    concurrent_scores[label] / duration_ms,
                )
                for label in concurrent_labels
                if label != primary_label
            )
        elif best_label is not None and best_ms > 0:
            decisions.append((best_label, "uncertain", best_ms, best_ratio))
            decisions.extend(
                (
                    label,
                    "overlap",
                    concurrent_scores[label],
                    concurrent_scores[label] / duration_ms,
                )
                for label in concurrent_labels
                if label != best_label
            )
        elif regular_scores:
            fallback_label, fallback_ms = max(
                regular_scores.items(), key=lambda item: (item[1], item[0])
            )
            decisions.append(
                (
                    fallback_label,
                    "uncertain",
                    fallback_ms,
                    fallback_ms / duration_ms,
                )
            )
        else:
            decisions.append((None, "none", 0, 0.0))
        for rank, (label, kind, overlap_ms, ratio) in enumerate(decisions):
            rows.append(
                {
                    "attribution_key": f"v2d:{run_id}:token:{token_id}:rank:{rank}",
                    "token_id": token_id,
                    "speaker_label": label,
                    "attribution_kind": kind,
                    "overlap_ms": overlap_ms,
                    "overlap_ratio": min(1.0, ratio),
                    "rank": rank,
                    "confidence": min(1.0, ratio),
                    "metadata": metadata,
                }
            )
    return rows


def compute_overlap_regions(
    turns: Sequence[SpeakerTurn],
) -> list[tuple[int, int, tuple[str, ...]]]:
    """Return half-open regions where at least two distinct speakers are active."""
    events: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for turn in turns:
        if turn.end_ms <= turn.start_ms:
            continue
        events[turn.start_ms].append((turn.speaker_label, 1))
        events[turn.end_ms].append((turn.speaker_label, -1))
    times = sorted(events)
    active: dict[str, int] = defaultdict(int)
    regions: list[tuple[int, int, tuple[str, ...]]] = []
    for index, time_ms in enumerate(times[:-1]):
        for label, delta in events[time_ms]:
            active[label] += delta
            if active[label] <= 0:
                active.pop(label, None)
        next_ms = times[index + 1]
        labels = tuple(sorted(active))
        if next_ms > time_ms and len(labels) >= 2:
            if regions and regions[-1][1] == time_ms and regions[-1][2] == labels:
                regions[-1] = (regions[-1][0], next_ms, labels)
            else:
                regions.append((time_ms, next_ms, labels))
    return regions


def snapshot_quality_diarization(
    database: Database,
    run_id: int,
    *,
    name: str | None = None,
    truth_set_id: int | None = None,
) -> QualityDiarizationSnapshotSummary:
    run = database.get_processing_run(run_id)
    if str(run["run_kind"]) != "quality_diarization_v2d":
        raise ValueError("processing run is not a V2-D diarization run")
    if str(run["status"]) != "completed":
        raise ValueError("only a completed V2-D run can be frozen")
    session_id = int(run["session_id"])
    input_fingerprint = str(run["input_fingerprint"])
    scopes: list[tuple[int, int]] | None = None
    adapter = "quality-diarization-v2d-overlap-aware-v1"
    if truth_set_id is not None:
        truth_set = database.get_truth_set(truth_set_id)
        if int(truth_set["session_id"]) != session_id:
            raise ValueError("V2-D run and truth set belong to different sessions")
        if str(truth_set["input_fingerprint"]) != input_fingerprint:
            raise ValueError("V2-D run and truth set input fingerprints differ")
        review_regions = [
            row
            for row in database.list_truth_annotations(truth_set_id)
            if str(row["label"] or "") == "review_region_complete_scope"
        ]
        scopes = (
            [
                (int(row["session_start_ms"]), int(row["session_end_ms"]))
                for row in review_regions
            ]
            if review_regions
            else [
                (int(truth_set["scope_start_ms"]), int(truth_set["scope_end_ms"]))
            ]
        )
        scopes.sort()
        for previous, current in zip(scopes, scopes[1:]):
            if current[0] < previous[1]:
                raise ValueError("truth review regions overlap")
        adapter += "-review-scoped"

    regular_rows = database.list_diarization_turns(run_id, turn_kind="regular")
    turns = [
        SpeakerTurn(
            start_ms=int(row["session_start_ms"]),
            end_ms=int(row["session_end_ms"]),
            speaker_label=str(row["speaker_label"]),
            confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
        )
        for row in regular_rows
    ]
    predictions: list[dict[str, Any]] = []
    speaker_predictions = 0
    for row, turn in zip(regular_rows, turns, strict=True):
        for scope_index, start_ms, end_ms in _clip_to_scopes(
            turn.start_ms, turn.end_ms, scopes
        ):
            suffix = "" if scope_index is None else f":truth:{truth_set_id}:scope:{scope_index}"
            predictions.append(
                {
                    "prediction_key": f"v2d:{run_id}:turn:{int(row['id'])}{suffix}",
                    "prediction_kind": "speaker",
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "label": turn.speaker_label,
                    "confidence": turn.confidence,
                    "metadata": {
                        "turn_id": int(row["id"]),
                        "turn_kind": "regular",
                        "original_session_start_ms": turn.start_ms,
                        "original_session_end_ms": turn.end_ms,
                        "scope_clipped": start_ms != turn.start_ms or end_ms != turn.end_ms,
                    },
                }
            )
            speaker_predictions += 1
    overlap_predictions = 0
    for overlap_index, (region_start, region_end, labels) in enumerate(
        compute_overlap_regions(turns)
    ):
        for scope_index, start_ms, end_ms in _clip_to_scopes(
            region_start, region_end, scopes
        ):
            suffix = "" if scope_index is None else f":truth:{truth_set_id}:scope:{scope_index}"
            predictions.append(
                {
                    "prediction_key": f"v2d:{run_id}:overlap:{overlap_index}{suffix}",
                    "prediction_kind": "overlap",
                    "session_start_ms": start_ms,
                    "session_end_ms": end_ms,
                    "label": "overlap",
                    "metadata": {"speaker_labels": list(labels)},
                }
            )
            overlap_predictions += 1
    manifest = json.loads(str(run["model_manifest_json"] or "{}"))
    if truth_set_id is not None:
        manifest["benchmark_scope"] = {
            "truth_set_id": truth_set_id,
            "review_regions": [
                {"start_ms": start, "end_ms": end} for start, end in scopes or []
            ],
            "boundary_policy": "intersect diarization interval with review region",
        }
    prediction_key = f"v2d:{run_id}:{adapter}"
    if truth_set_id is not None:
        prediction_key += f":truth:{truth_set_id}"
    prediction_set = database.create_benchmark_prediction_set(
        {
            "prediction_key": prediction_key,
            "name": name or f"v2-d-run-{run_id}",
            "session_id": session_id,
            "processing_run_id": run_id,
            "input_fingerprint": input_fingerprint,
            "adapter": adapter,
            "model_manifest": manifest,
        },
        predictions,
    )
    return QualityDiarizationSnapshotSummary(
        prediction_set_id=int(prediction_set["id"]),
        prediction_count=len(predictions),
        speaker_predictions=speaker_predictions,
        overlap_predictions=overlap_predictions,
        content_sha256=str(prediction_set["content_sha256"]),
    )


def _clip_turns(turns: Iterable[SpeakerTurn], duration_ms: int) -> list[SpeakerTurn]:
    clipped: list[SpeakerTurn] = []
    for turn in turns:
        start_ms = max(0, min(duration_ms, int(turn.start_ms)))
        end_ms = max(0, min(duration_ms, int(turn.end_ms)))
        if end_ms <= start_ms:
            continue
        metadata = dict(turn.metadata or {})
        if start_ms != turn.start_ms or end_ms != turn.end_ms:
            metadata["session_boundary_clipped"] = True
        clipped.append(
            SpeakerTurn(
                start_ms=start_ms,
                end_ms=end_ms,
                speaker_label=turn.speaker_label,
                confidence=turn.confidence,
                metadata=metadata,
            )
        )
    return sorted(clipped, key=lambda turn: (turn.start_ms, turn.end_ms, turn.speaker_label))


def _trace_turns(
    database: Database,
    run_id: int,
    session_id: int,
    regular: Sequence[SpeakerTurn],
    exclusive: Sequence[SpeakerTurn],
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for kind, turns in (("regular", regular), ("exclusive", exclusive)):
        for index, turn in enumerate(turns):
            slices, gaps = resolve_session_slices(
                database, session_id, turn.start_ms, turn.end_ms
            )
            if gaps:
                raise ValueError(f"speaker turn crosses an uncovered source range: {gaps}")
            values.append(
                {
                    "turn_key": f"v2d:{run_id}:{kind}:{index}",
                    "turn_index": index,
                    "turn_kind": kind,
                    "speaker_label": turn.speaker_label,
                    "session_start_ms": turn.start_ms,
                    "session_end_ms": turn.end_ms,
                    "confidence": turn.confidence,
                    "metadata": turn.metadata or {},
                    "source_refs": [
                        {
                            "source_object_id": item.source_object_id,
                            "source_instance_id": item.source_instance_id,
                            "source_sha256": item.source_sha256,
                            "source_start_ms": item.source_start_ms,
                            "source_end_ms": item.source_end_ms,
                        }
                        for item in slices
                    ],
                }
            )
    return values


def _speaker_overlap_scores(
    start_ms: int, end_ms: int, turns: Sequence[SpeakerTurn]
) -> dict[str, int]:
    intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for turn in turns:
        start = max(start_ms, turn.start_ms)
        end = min(end_ms, turn.end_ms)
        if end > start:
            intervals[turn.speaker_label].append((start, end))
    return {
        label: sum(end - start for start, end in _merge_intervals(values))
        for label, values in intervals.items()
    }


def _concurrent_speaker_scores(
    start_ms: int,
    end_ms: int,
    regions: Sequence[tuple[int, int, tuple[str, ...]]],
) -> dict[str, int]:
    scores: dict[str, int] = defaultdict(int)
    for region_start, region_end, labels in regions:
        overlap_ms = max(0, min(end_ms, region_end) - max(start_ms, region_start))
        if overlap_ms:
            for label in labels:
                scores[label] += overlap_ms
    return dict(scores)


def _merge_intervals(values: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(values):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clip_to_scopes(
    start_ms: int,
    end_ms: int,
    scopes: Sequence[tuple[int, int]] | None,
) -> list[tuple[int | None, int, int]]:
    if scopes is None:
        return [(None, start_ms, end_ms)]
    return [
        (index, max(start_ms, scope_start), min(end_ms, scope_end))
        for index, (scope_start, scope_end) in enumerate(scopes)
        if start_ms < scope_end and end_ms > scope_start
    ]


def _backend_manifest(backend: QualityDiarizationBackend) -> dict[str, Any]:
    return {
        "model_id": backend.model_id,
        "model_revision": backend.model_revision,
        "backend": backend.backend_name,
        "parameters": backend.parameters(),
        "privacy": "local-inference-no-audio-upload",
        "outputs": ["overlap-aware", "exclusive"],
    }


def _write_manifest(
    database: Database,
    run_id: int,
    settings: QualityDiarizationSettings,
    raw_response: dict[str, Any],
    summary: dict[str, Any],
) -> Path:
    run = database.get_processing_run(run_id)
    output_dir = OUTPUT_DIR / f"session-{int(run['session_id']):06d}" / "diarization-v2d"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"run-{run_id:06d}.json"
    turns = database.list_diarization_turns(run_id)
    payload = {
        "format": "allday-recording-asr-v2d-run-v1",
        "run_id": run_id,
        "session_id": int(run["session_id"]),
        "input_fingerprint": str(run["input_fingerprint"]),
        "settings": settings.to_dict(),
        "model_manifest": json.loads(str(run["model_manifest_json"] or "{}")),
        "backend_summary": raw_response,
        "summary": summary,
        "turn_content_sha256": [str(row["content_sha256"]) for row in turns],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
