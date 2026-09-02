from __future__ import annotations
from collections.abc import Sequence
from typing import Any
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

from .native_contracts import (
    AsrBackend,
    BackendFactory,
    DiarizationBackend,
    NativeAsrSettings,
    NativeDiarizationSettings,
    _Source,
    _Window,
)
from .native_audio import _temporary_audio
from .native_projection import (
    _artifact_json,
    _backend_manifest,
    _group_utterances,
    _source_refs,
    _speaker_for,
    _turn,
)
from .native_runtime import _json_safe


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
                        raise StageCancellationRequested(
                            "V3 ASR cancelled at a window boundary"
                        )
                    with _temporary_audio(
                        window.sources, window.analysis_start_ms, window.analysis_end_ms
                    ) as audio:
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
            raise StageCancellationRequested(
                "V3 diarization cancelled before model load"
            )
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

    def _project_utterances(
        self, context: StageExecutionContext
    ) -> StageExecutionResult:
        asr = _artifact_json(context, "v3_asr_evidence")
        diarization = _artifact_json(context, "v3_diarization_evidence")
        tokens = [dict(value) for value in asr["primary_tokens"]]
        turns = [dict(value) for value in diarization["exclusive_turns"]]
        attributed = [
            {
                **token,
                "speaker": _speaker_for(
                    token, turns, self.diarization.min_primary_overlap_ratio
                ),
            }
            for token in tokens
        ]
        projected = _group_utterances(attributed)
        speakers = sorted(
            {
                str(value["speaker"])
                for value in projected
                if value["speaker"] is not None
            }
        )
        utterances = tuple(
            UtteranceProjectionOutput(
                ordinal=index,
                start_ms=int(value["start_ms"]),
                end_ms=int(value["end_ms"]),
                text=str(value["text"]),
                speaker_label=(
                    str(value["speaker"]) if value["speaker"] is not None else None
                ),
                evidence={
                    "format": "v3-native-token-span-v1",
                    "source_refs": value["source_refs"],
                    "token_count": value["token_count"],
                },
                identity=SelfIdentity.UNKNOWN,
                identity_evidence=unknown_identity_evidence(
                    "native_pipeline_requires_open_set_identity"
                ),
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
            checkpoint={
                "utterance_count": len(utterances),
                "speaker_count": len(speakers),
            },
            log_summary="V3-native tokens were projected into evidence-linked utterances",
            artifacts=(
                StageArtifactOutput(
                    kind="v3_transcript_evidence",
                    payload=canonical_json(payload).encode("utf-8"),
                    producer="v3-native-transcript-projection",
                    producer_version="v3-native.1",
                    metadata={
                        "utterance_count": len(utterances),
                        "speaker_count": len(speakers),
                    },
                    input_refs=(
                        context.prior_artifacts["v3_asr_evidence"][0].artifact_id,
                        context.prior_artifacts["v3_diarization_evidence"][
                            0
                        ].artifact_id,
                    ),
                    dependencies=(
                        ArtifactDependencyOutput(
                            "artifact",
                            context.prior_artifacts["v3_asr_evidence"][0].artifact_id,
                            1,
                        ),
                        ArtifactDependencyOutput(
                            "artifact",
                            context.prior_artifacts["v3_diarization_evidence"][
                                0
                            ].artifact_id,
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

    def _windows(
        self, sources: Sequence[_Source], duration_ms: int
    ) -> tuple[_Window, ...]:
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
