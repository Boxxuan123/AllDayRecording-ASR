from __future__ import annotations

import json
import os
import tempfile
import uuid
import wave
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

from allday_asr.v3.adapters.audio.tools import extract_clip
from allday_asr.v3.domain.hashing import canonical_json
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.identity import SelfIdentity, unknown_identity_evidence
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


class AsrBackend(Protocol):
    role: str
    model_id: str
    backend_name: str
    model_revision: str | None

    def transcribe(self, audio_path: Path, *, language: str | None) -> Any: ...

    def parameters(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class DiarizationBackend(Protocol):
    model_id: str
    backend_name: str
    model_revision: str | None

    def ensure_loaded(self) -> None: ...

    def diarize(self, audio_path: Path, **kwargs: Any) -> Any: ...

    def parameters(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


BackendFactory = Callable[[], Any]


@dataclass(frozen=True)
class NativeAsrSettings:
    language: str = "zh"
    window_ms: int = 300_000
    context_ms: int = 5_000


@dataclass(frozen=True)
class NativeDiarizationSettings:
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    min_primary_overlap_ratio: float = 0.50


@dataclass(frozen=True)
class _Source:
    asset_id: str
    path: Path
    sha256: str
    session_start_ms: int
    session_end_ms: int
    source_start_ms: int
    source_end_ms: int


@dataclass(frozen=True)
class _Window:
    index: int
    core_start_ms: int
    core_end_ms: int
    analysis_start_ms: int
    analysis_end_ms: int
    sources: tuple[_Source, ...]


class NativeModelPipelineAdapter:
    """Run the V3-native ASR and diarization pipeline.

    The model implementations are shared infrastructure. Session identity, job
    state, immutable evidence, transcript projection and source coordinates are
    all V3-owned.
    """

    def __init__(
        self,
        database: V3Database,
        audio_store: ContentAddressedStore,
        *,
        asr: NativeAsrSettings,
        diarization: NativeDiarizationSettings,
        primary_factory: BackendFactory,
        secondary_factory: BackendFactory,
        diarization_factory: BackendFactory,
    ) -> None:
        if asr.window_ms < 1 or asr.context_ms < 0:
            raise ValueError("V3 ASR window settings are invalid")
        self.database = database
        self.audio_store = audio_store
        self.asr = asr
        self.diarization = diarization
        self.primary_factory = primary_factory
        self.secondary_factory = secondary_factory
        self.diarization_factory = diarization_factory

    def execute(
        self,
        context: StageExecutionContext,
        control: StageExecutionControl,
    ) -> StageExecutionResult:
        stage = context.claim.stage.stage
        session_id = context.claim.run.session_id
        if stage == "ingest_verified":
            sources, duration_ms = self._sources(session_id)
            return StageExecutionResult(
                checkpoint={"source_count": len(sources), "duration_ms": duration_ms},
                log_summary="V3 manifest, content digests and continuous audio were verified",
            )
        if stage == "backup_admitted":
            mode = str(context.claim.job.request.get("admission_mode") or "production")
            return StageExecutionResult(
                checkpoint={"admitted": mode == "production", "mode": mode},
                log_summary=(
                    "V3 independent backup admission is satisfied"
                    if mode == "production"
                    else "V3 shadow run retained the independent-backup blocker"
                ),
            )
        if stage == "window_plan":
            sources, duration_ms = self._sources(session_id)
            windows = self._windows(sources, duration_ms)
            return StageExecutionResult(
                checkpoint={"window_count": len(windows), "duration_ms": duration_ms},
                log_summary="V3 logical model windows were planned from native audio assets",
            )
        if stage == "speech_gate":
            return StageExecutionResult(
                checkpoint={"policy": "backend-evidence-gate"},
                log_summary="Speech gating is bound to the primary V3 ASR execution",
            )
        if stage == "asr_and_alignment":
            return self._run_asr(context, control)
        if stage == "diarization":
            return self._run_diarization(context, control)
        if stage == "utterance_projection":
            return self._project_utterances(context)
        if stage == "semantic_evidence_optional":
            return self._semantic_seed(context)
        if stage == "mobile_projection":
            return StageExecutionResult(
                checkpoint={"projection": "change-log"},
                log_summary="V3 transcript changes are ready for device synchronization",
            )
        raise ValueError(f"unsupported V3 processing stage: {stage}")

    def _run_asr(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        sources, duration_ms = self._sources(context.claim.run.session_id)
        windows = self._windows(sources, duration_ms)
        hypotheses: list[dict[str, Any]] = []
        all_tokens: dict[str, list[dict[str, Any]]] = {"primary": [], "secondary": []}
        manifests: dict[str, dict[str, Any]] = {}
        for role, factory in (
            ("primary", self.primary_factory),
            ("secondary", self.secondary_factory),
        ):
            backend: AsrBackend = factory()
            if backend.role != role:
                raise ValueError(f"V3 ASR backend role mismatch: {role}")
            try:
                for window in windows:
                    if control.heartbeat({"role": role, "window": window.index}):
                        raise StageCancellationRequested("V3 ASR cancelled at a window boundary")
                    with _temporary_audio(window.sources, window.analysis_start_ms, window.analysis_end_ms) as audio:
                        result = backend.transcribe(audio, language=self.asr.language)
                    tokens = self._tokens(result.tokens, window, sources)
                    all_tokens[role].extend(tokens)
                    hypotheses.append(
                        {
                            "role": role,
                            "window_index": window.index,
                            "core_start_ms": window.core_start_ms,
                            "core_end_ms": window.core_end_ms,
                            "analysis_start_ms": window.analysis_start_ms,
                            "analysis_end_ms": window.analysis_end_ms,
                            "text": str(result.text or ""),
                            "language": result.language,
                            "token_count": len(tokens),
                            "raw_response": _json_safe(result.raw_response),
                        }
                    )
                manifests[role] = _backend_manifest(backend)
            finally:
                backend.close()
        snapshot = {
            "format": "AllDayRecording V3 ASR evidence v1",
            "session_id": context.claim.run.session_id,
            "duration_ms": duration_ms,
            "window_count": len(windows),
            "models": manifests,
            "hypotheses": hypotheses,
            "primary_tokens": all_tokens["primary"],
            "secondary_tokens": all_tokens["secondary"],
        }
        dependencies = tuple(
            ArtifactDependencyOutput("audio_asset", source.asset_id, 1)
            for source in sources
        )
        return StageExecutionResult(
            checkpoint={
                "window_count": len(windows),
                "primary_tokens": len(all_tokens["primary"]),
                "secondary_tokens": len(all_tokens["secondary"]),
            },
            log_summary="V3-native dual-model ASR and alignment completed",
            artifacts=(
                StageArtifactOutput(
                    kind="v3_asr_evidence",
                    payload=canonical_json(snapshot).encode("utf-8"),
                    producer="v3-native-asr",
                    producer_version="v3-native.1",
                    metadata={
                        "window_count": len(windows),
                        "primary_token_count": len(all_tokens["primary"]),
                        "secondary_token_count": len(all_tokens["secondary"]),
                    },
                    input_refs=tuple(source.asset_id for source in sources),
                    dependencies=dependencies,
                ),
            ),
        )

    def _run_diarization(
        self, context: StageExecutionContext, control: StageExecutionControl
    ) -> StageExecutionResult:
        sources, duration_ms = self._sources(context.claim.run.session_id)
        if control.heartbeat({"stage": "diarization", "status": "loading"}):
            raise StageCancellationRequested("V3 diarization cancelled before model load")
        backend: DiarizationBackend = self.diarization_factory()
        try:
            backend.ensure_loaded()
            with _temporary_audio(sources, 0, duration_ms) as audio:
                result = backend.diarize(
                    audio,
                    num_speakers=self.diarization.num_speakers,
                    min_speakers=self.diarization.min_speakers,
                    max_speakers=self.diarization.max_speakers,
                )
            regular = [_turn(value, duration_ms) for value in result.regular_turns]
            exclusive = [_turn(value, duration_ms) for value in result.exclusive_turns]
            regular = [value for value in regular if value is not None]
            exclusive = [value for value in exclusive if value is not None]
            snapshot = {
                "format": "AllDayRecording V3 diarization evidence v1",
                "session_id": context.claim.run.session_id,
                "duration_ms": duration_ms,
                "model": _backend_manifest(backend),
                "regular_turns": regular,
                "exclusive_turns": exclusive,
                "raw_response": _json_safe(result.raw_response),
            }
        finally:
            backend.close()
        dependencies = tuple(
            ArtifactDependencyOutput("audio_asset", source.asset_id, 1)
            for source in sources
        )
        return StageExecutionResult(
            checkpoint={
                "regular_turn_count": len(regular),
                "exclusive_turn_count": len(exclusive),
                "speaker_count": len({value["speaker_label"] for value in regular}),
            },
            log_summary="V3-native speaker diarization completed",
            artifacts=(
                StageArtifactOutput(
                    kind="v3_diarization_evidence",
                    payload=canonical_json(snapshot).encode("utf-8"),
                    producer="v3-native-diarization",
                    producer_version="v3-native.1",
                    metadata={
                        "regular_turn_count": len(regular),
                        "exclusive_turn_count": len(exclusive),
                    },
                    input_refs=tuple(source.asset_id for source in sources),
                    dependencies=dependencies,
                ),
            ),
        )

    def _project_utterances(self, context: StageExecutionContext) -> StageExecutionResult:
        asr = _artifact_json(context, "v3_asr_evidence")
        diarization = _artifact_json(context, "v3_diarization_evidence")
        tokens = [dict(value) for value in asr["primary_tokens"]]
        turns = [dict(value) for value in diarization["exclusive_turns"]]
        attributed = [
            {**token, "speaker": _speaker_for(token, turns, self.diarization.min_primary_overlap_ratio)}
            for token in tokens
        ]
        projected = _group_utterances(attributed)
        speakers = sorted(
            {str(value["speaker"]) for value in projected if value["speaker"] is not None}
        )
        utterances = tuple(
            UtteranceProjectionOutput(
                ordinal=index,
                start_ms=int(value["start_ms"]),
                end_ms=int(value["end_ms"]),
                text=str(value["text"]),
                speaker_label=(str(value["speaker"]) if value["speaker"] is not None else None),
                evidence={
                    "format": "v3-native-token-span-v1",
                    "source_refs": value["source_refs"],
                    "token_count": value["token_count"],
                },
                identity=SelfIdentity.UNKNOWN,
                identity_evidence=unknown_identity_evidence("native_pipeline_requires_open_set_identity"),
            )
            for index, value in enumerate(projected)
        )
        payload = {
            "format": "AllDayRecording V3 transcript evidence v1",
            "session_id": context.claim.run.session_id,
            "utterances": projected,
            "speaker_labels": speakers,
        }
        return StageExecutionResult(
            checkpoint={"utterance_count": len(utterances), "speaker_count": len(speakers)},
            log_summary="V3-native tokens were projected into evidence-linked utterances",
            artifacts=(
                StageArtifactOutput(
                    kind="v3_transcript_evidence",
                    payload=canonical_json(payload).encode("utf-8"),
                    producer="v3-native-transcript-projection",
                    producer_version="v3-native.1",
                    metadata={"utterance_count": len(utterances), "speaker_count": len(speakers)},
                    input_refs=(
                        context.prior_artifacts["v3_asr_evidence"][0].artifact_id,
                        context.prior_artifacts["v3_diarization_evidence"][0].artifact_id,
                    ),
                    dependencies=(
                        ArtifactDependencyOutput(
                            "artifact",
                            context.prior_artifacts["v3_asr_evidence"][0].artifact_id,
                            1,
                        ),
                        ArtifactDependencyOutput(
                            "artifact",
                            context.prior_artifacts["v3_diarization_evidence"][0].artifact_id,
                            1,
                        ),
                    ),
                ),
            ),
            speakers=tuple(SpeakerProjectionOutput(value) for value in speakers),
            utterances=utterances,
        )

    def _semantic_seed(self, context: StageExecutionContext) -> StageExecutionResult:
        transcript = _artifact_json(context, "v3_transcript_evidence")
        dependencies = tuple(
            ArtifactDependencyOutput(
                "utterance",
                stable_ulid("utterance", context.claim.run.run_id, index),
                1,
            )
            for index, _ in enumerate(transcript["utterances"])
        )
        return StageExecutionResult(
            checkpoint={"utterance_count": len(dependencies)},
            log_summary="V3 semantic generation inputs were frozen for Codex",
            artifacts=(
                StageArtifactOutput(
                    kind="v3_semantic_input",
                    payload=canonical_json(transcript).encode("utf-8"),
                    producer="v3-native-semantic-input",
                    producer_version="v3-native.1",
                    input_refs=tuple(value.input_id for value in dependencies),
                    dependencies=dependencies,
                ),
            ),
        )

    def _sources(self, session_id: str) -> tuple[tuple[_Source, ...], int]:
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT captured_start, captured_end FROM recording_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            rows = connection.execute(
                """
                SELECT s.sequence, s.session_start_ms, s.session_end_ms,
                       s.source_start_ms, s.source_end_ms,
                       a.asset_id, a.sha256, r.storage_key
                FROM capture_segments s
                JOIN audio_assets a ON a.asset_id = s.asset_id
                JOIN audio_replicas r ON r.replica_id = s.replica_id
                WHERE s.session_id = ? AND r.state = 'available'
                ORDER BY s.sequence, s.segment_id
                """,
                (session_id,),
            ).fetchall()
        if session is None or not rows:
            raise ValueError("V3 session has no available audio graph")
        sources = tuple(
            _Source(
                asset_id=str(row["asset_id"]),
                path=self.audio_store.path_for(str(row["storage_key"])),
                sha256=str(row["sha256"]),
                session_start_ms=int(row["session_start_ms"]),
                session_end_ms=int(row["session_end_ms"]),
                source_start_ms=int(row["source_start_ms"]),
                source_end_ms=int(row["source_end_ms"]),
            )
            for row in rows
        )
        normalized: list[_Source] = []
        cursor = 0
        for source in sources:
            duration_ms = source.session_end_ms - source.session_start_ms
            if abs(source.session_start_ms - cursor) > 1 or duration_ms <= 0:
                raise ValueError("V3 session audio graph contains a gap or overlap")
            if not source.path.is_file():
                raise FileNotFoundError(f"V3 audio content is missing: {source.sha256}")
            normalized.append(
                _Source(
                    asset_id=source.asset_id,
                    path=source.path,
                    sha256=source.sha256,
                    session_start_ms=cursor,
                    session_end_ms=cursor + duration_ms,
                    source_start_ms=source.source_start_ms,
                    source_end_ms=source.source_end_ms,
                )
            )
            cursor += duration_ms
        return tuple(normalized), cursor

    def _windows(self, sources: Sequence[_Source], duration_ms: int) -> tuple[_Window, ...]:
        values: list[_Window] = []
        core_start = 0
        while core_start < duration_ms:
            core_end = min(duration_ms, core_start + self.asr.window_ms)
            analysis_start = max(0, core_start - self.asr.context_ms)
            analysis_end = min(duration_ms, core_end + self.asr.context_ms)
            values.append(
                _Window(
                    index=len(values),
                    core_start_ms=core_start,
                    core_end_ms=core_end,
                    analysis_start_ms=analysis_start,
                    analysis_end_ms=analysis_end,
                    sources=tuple(
                        value
                        for value in sources
                        if value.session_end_ms > analysis_start
                        and value.session_start_ms < analysis_end
                    ),
                )
            )
            core_start = core_end
        return tuple(values)

    def _tokens(
        self, raw_tokens: Sequence[Any], window: _Window, sources: Sequence[_Source]
    ) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for raw in raw_tokens:
            start_ms = window.analysis_start_ms + round(float(raw.start_seconds) * 1000)
            end_ms = window.analysis_start_ms + round(float(raw.end_seconds) * 1000)
            midpoint = (start_ms + end_ms) // 2
            metadata = dict(raw.metadata or {})
            if end_ms <= start_ms or not (
                window.core_start_ms <= midpoint < window.core_end_ms
            ):
                continue
            if metadata.get("speech_gate_committed") is False:
                continue
            refs = _source_refs(sources, start_ms, end_ms)
            if not refs:
                continue
            values.append(
                {
                    "ordinal": len(values),
                    "text": str(raw.text),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "confidence": raw.confidence,
                    "metadata": metadata,
                    "source_refs": refs,
                }
            )
        return values


def build_native_model_pipeline(
    database: V3Database,
    audio_store: ContentAddressedStore,
    config: Any,
    *,
    requested_profile: str | None = None,
    diarization_model_path: Path | None = None,
    builders: Mapping[str, Callable[..., Any]] | None = None,
) -> NativeModelPipelineAdapter:
    """Compose local model backends while keeping all execution state in V3."""

    if builders is None:
        from allday_asr.v3.adapters.models.asr_backends import (
            FunAsrNanoBackend,
            Qwen3AsrBackend,
            SpeechGateSettings,
        )
        from allday_asr.v3.adapters.models.diarization import PyannoteCommunityBackend

        builders = {
            "speech_gate": SpeechGateSettings,
            "primary": Qwen3AsrBackend,
            "secondary": FunAsrNanoBackend,
            "diarization": PyannoteCommunityBackend,
        }
    profile = requested_profile or config.asr.vram_profile
    profile = _resolve_profile(profile, device=config.runtime.device)
    batch_size = (
        config.asr.primary_batch_size_16gb
        if profile == "quality-16gb"
        else config.asr.primary_batch_size_8gb
    )
    gate = builders["speech_gate"](
        fsmn_merge_gap_ms=config.asr.speech_gate_fsmn_merge_gap_ms,
        max_utterance_ms=config.asr.speech_gate_max_utterance_ms,
        inference_padding_ms=config.asr.speech_gate_inference_padding_ms,
        speech_output_padding_ms=config.asr.speech_gate_output_padding_ms,
        min_candidate_ms=config.asr.speech_gate_min_candidate_ms,
        min_snr_db=config.asr.speech_gate_min_snr_db,
        silero_threshold=config.asr.speech_gate_silero_threshold,
        silero_min_speech_ms=config.asr.speech_gate_silero_min_speech_ms,
        silero_min_silence_ms=config.asr.speech_gate_silero_min_silence_ms,
        min_silero_overlap_ms=config.asr.speech_gate_min_silero_overlap_ms,
    )

    def primary() -> Any:
        return builders["primary"](
            model_id=config.asr.primary_model,
            aligner_model_id=config.asr.forced_aligner_model,
            device=config.runtime.device,
            batch_size=batch_size,
            max_new_tokens=config.asr.max_new_tokens,
            speech_gate=gate,
        )

    def secondary() -> Any:
        return builders["secondary"](
            model_id=config.asr.secondary_model,
            device=config.runtime.device,
        )

    def diarization() -> Any:
        quality = config.diarization
        selected_path = diarization_model_path or (
            Path(quality.model_path).resolve() if quality.model_path else None
        )
        return builders["diarization"](
            model_id=quality.model_id,
            model_path=selected_path,
            device=config.runtime.device,
            token_env=quality.token_env,
        )

    return NativeModelPipelineAdapter(
        database,
        audio_store,
        asr=NativeAsrSettings(
            language=config.asr.language,
            window_ms=round(config.asr.window_seconds * 1000),
            context_ms=round(config.asr.context_seconds * 1000),
        ),
        diarization=NativeDiarizationSettings(
            num_speakers=config.diarization.num_speakers,
            min_speakers=config.diarization.min_speakers,
            max_speakers=config.diarization.max_speakers,
            min_primary_overlap_ratio=config.diarization.min_primary_overlap_ratio,
        ),
        primary_factory=primary,
        secondary_factory=secondary,
        diarization_factory=diarization,
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
        overlap = max(0, min(end, int(turn["end_ms"])) - max(start, int(turn["start_ms"])))
        if overlap:
            label = str(turn["speaker_label"])
            scores[label] = scores.get(label, 0) + overlap
    if not scores:
        return None
    label, overlap = max(scores.items(), key=lambda item: (item[1], item[0]))
    return label if overlap / duration >= threshold else None


def _group_utterances(tokens: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for token in sorted(tokens, key=lambda item: (int(item["start_ms"]), int(item["end_ms"]))):
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
            if (value["asset_id"], value["source_start_ms"], value["source_end_ms"]) not in known
        )
    return groups


def _join_text(left: str, right: str) -> str:
    if not left or not right:
        return left + right
    if left[-1].isascii() and right[0].isascii() and left[-1].isalnum() and right[0].isalnum():
        return f"{left} {right}"
    return left + right


def _source_refs(sources: Sequence[_Source], start_ms: int, end_ms: int) -> list[dict[str, Any]]:
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


@contextmanager
def _temporary_audio(
    sources: Sequence[_Source], start_ms: int, end_ms: int
) -> Iterator[Path]:
    root = Path(tempfile.gettempdir())
    token = uuid.uuid4().hex
    output = root / f"allday-v3-{token}.wav"
    parts: list[Path] = []
    combined = root / f"allday-v3-{token}.combined.wav"
    try:
        cursor = start_ms
        for index, source in enumerate(sources):
            overlap_start = max(start_ms, source.session_start_ms)
            overlap_end = min(end_ms, source.session_end_ms)
            if overlap_end <= overlap_start:
                continue
            if overlap_start != cursor:
                raise ValueError("V3 model window has an uncovered audio range")
            part = root / f"allday-v3-{token}-{index:04d}.wav"
            source_start = source.source_start_ms + overlap_start - source.session_start_ms
            extract_clip(
                source.path,
                part,
                source_start,
                source_start + overlap_end - overlap_start,
            )
            parts.append(part)
            cursor = overlap_end
        if cursor != end_ms or not parts:
            raise ValueError("V3 model window is incomplete")
        if len(parts) == 1:
            os.replace(parts[0], output)
        else:
            _concatenate(parts, combined)
            os.replace(combined, output)
        yield output
    finally:
        output.unlink(missing_ok=True)
        combined.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)


def _concatenate(parts: Sequence[Path], destination: Path) -> None:
    parameters: tuple[int, int, int, str, str] | None = None
    with wave.open(str(destination), "wb") as writer:
        for part in parts:
            with wave.open(str(part), "rb") as reader:
                current = (
                    reader.getnchannels(),
                    reader.getsampwidth(),
                    reader.getframerate(),
                    reader.getcomptype(),
                    reader.getcompname(),
                )
                if parameters is None:
                    parameters = current
                    writer.setparams((*current[:3], 0, *current[3:]))
                elif current != parameters:
                    raise RuntimeError("V3 audio chunks do not share one WAV format")
                writer.writeframes(reader.readframes(reader.getnframes()))


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _resolve_profile(requested: str, *, device: str) -> str:
    if requested in {"quality-16gb", "compatible-8gb"}:
        return requested
    if requested != "auto":
        raise ValueError("V3 model profile must be auto, quality-16gb or compatible-8gb")
    if device.strip().lower() == "cpu":
        return "compatible-8gb"
    try:
        import torch

        if not torch.cuda.is_available():
            return "compatible-8gb"
        index = int(device.split(":", 1)[1]) if device.startswith("cuda:") else torch.cuda.current_device()
        total_bytes = int(torch.cuda.get_device_properties(index).total_memory)
    except (ImportError, RuntimeError, ValueError):
        return "compatible-8gb"
    return "quality-16gb" if total_bytes >= 14 * 1024**3 else "compatible-8gb"


__all__ = [
    "NativeAsrSettings",
    "NativeDiarizationSettings",
    "NativeModelPipelineAdapter",
    "build_native_model_pipeline",
]
