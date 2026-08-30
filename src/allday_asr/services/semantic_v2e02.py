from __future__ import annotations

import json
import os
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from allday_asr.domain.hashing import canonical_json as _canonical_json
from allday_asr.domain.hashing import canonical_json_sha256 as _sha256_json
from allday_asr.paths import OUTPUT_DIR
from allday_asr.services.semantic_v2e0 import (
    resolve_semantic_input_runs,
    semantic_tokens,
)
from allday_asr.services.semantic_v2e01 import (
    SemanticV2E01Settings,
    build_conversation_evidence,
)
from allday_asr.services.semantic_v2e01 import (
    semantic_overview as v2e01_semantic_overview,
)
from allday_asr.storage.database import Database

EVIDENCE_LEDGER_FORMAT = "AllDayRecording local semantic evidence ledger v3"
LLM_TRANSPORT_FORMAT = "AllDayRecording LLM episode transport plan v2"
LLM_REQUEST_FORMAT = "AllDayRecording LLM episode semantic request v2"
SEMANTIC_RESPONSE_FORMAT = "AllDayRecording semantic scene response v3"
MANUAL_BUNDLE_FORMAT = "AllDayRecording Codex manual semantic bundle v1"

SOURCE_LABELS = frozenset(
    {"live_person", "media_playback", "mixed_live_media", "unknown"}
)
NON_IDENTITY_LABELS = frozenset(
    {"", "unknown", "not_self", "tv", "media", "media_playback"}
)
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
        "truth_set_id",
        "reference_id",
        "database_id",
    }
)


@dataclass(frozen=True)
class SemanticV2E02Settings:
    episode_gap_ms: int = 180_000
    utterance_gap_ms: int = 2_500
    min_informative_chars: int = 4
    max_llm_request_chars: int = 200_000
    chunk_overlap_utterances: int = 6
    review_clip_ms: int = 120_000
    transcript_preview_chars: int = 600
    evidence_resolution_min_ratio: float = 0.50
    evidence_resolution_min_margin: float = 0.15

    def validate(self) -> None:
        if not 30_000 <= self.episode_gap_ms <= 900_000:
            raise ValueError("episode_gap_ms 必须在 30 秒到 15 分钟之间")
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
        if not 0.0 < self.evidence_resolution_min_ratio <= 1.0:
            raise ValueError("evidence_resolution_min_ratio 必须在 0 到 1 之间")
        if not 0.0 <= self.evidence_resolution_min_margin <= 1.0:
            raise ValueError("evidence_resolution_min_margin 必须在 0 到 1 之间")


@dataclass(frozen=True)
class SemanticV2E02Summary:
    run_id: int
    asr_run_id: int
    diarization_run_id: int | None
    episode_count: int
    excluded_block_count: int
    llm_job_count: int
    llm_payload_bytes: int
    token_count: int
    candidate_count: int
    scene_count: int
    claim_count: int
    action_count: int
    unresolved_count: int
    request_sha256: str
    provider_request_sha256: str
    response_sha256: str
    manifest_path: Path


class SemanticProvider(Protocol):
    provider_name: str
    model_name: str
    network_access: bool
    manual_eval: bool

    def generate(self, request: dict[str, Any]) -> dict[str, Any]: ...


class EpisodeContractMockProvider:
    provider_name = "local_mock"
    model_name = "episode-contract-v3"
    network_access = False
    manual_eval = False

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        episodes = assemble_transport_episodes(request)
        start_ms = int(episodes[0]["start_ms"]) if episodes else 0
        end_ms = (
            int(episodes[-1]["end_ms"])
            if episodes
            else int(request["session"]["duration_ms"])
        )
        return {
            "format": SEMANTIC_RESPONSE_FORMAT,
            "request_sha256": _sha256_json(request),
            "mode": "contract_mock_no_semantic_inference",
            "daily_summary": {
                "title": "V2-E.0.2 Episode 证据计划",
                "body": (
                    f"已准备 {len(episodes)} 个 episode；当前是契约 mock，"
                    "没有生成 scene、claim 或 action。"
                ),
                "evidence_scene_ids": [],
            },
            "scenes": [],
            "claims": [],
            "actions": [],
            "unresolved": [
                {
                    "id": f"unresolved-{index:04d}",
                    "episode_id": str(episode["key"]),
                    "description": "尚未由语义模型解释该 episode。",
                    "evidence_utterance_ids": [
                        str(item["id"])
                        for item in episode["utterances"][:1]
                    ],
                }
                for index, episode in enumerate(episodes, start=1)
                if episode["utterances"]
            ],
            "coverage": {"start_ms": start_ms, "end_ms": end_ms},
        }


class ReplaySemanticProvider:
    network_access = False
    manual_eval = True

    def __init__(
        self,
        response: dict[str, Any],
        *,
        provider_name: str = "codex_manual_eval",
        model_name: str = "codex-session-unversioned",
    ) -> None:
        self.response = response
        self.provider_name = provider_name
        self.model_name = model_name

    def generate(self, request: dict[str, Any]) -> dict[str, Any]:
        return json.loads(json.dumps(self.response, ensure_ascii=False))


@dataclass(frozen=True)
class PreparedSemanticInput:
    recording_id: int | None
    session_id: int
    duration_ms: int
    asr_run_id: int
    diarization_run_id: int | None
    tokens: list[dict[str, Any]]
    episodes: list[dict[str, Any]]
    excluded_blocks: list[dict[str, Any]]
    provider_request: dict[str, Any]


def prepare_semantic_v2e02(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
    settings: SemanticV2E02Settings | None = None,
) -> PreparedSemanticInput:
    settings = settings or SemanticV2E02Settings()
    settings.validate()
    asr_run, diarization_run = resolve_semantic_input_runs(
        database,
        recording_id,
        session_id=session_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
    )
    if session_id is None:
        if recording_id is None:
            raise ValueError("V2-E.0.2 必须指定 recording_id 或 session_id")
        session = database.get_session_for_recording(recording_id)
        session_id = int(session["id"])
    else:
        session = database.get_recording_session(session_id)
    resolved_diarization_id = (
        int(diarization_run["id"]) if diarization_run is not None else None
    )
    tokens = semantic_tokens(
        database,
        int(asr_run["id"]),
        resolved_diarization_id,
    )
    if not tokens:
        raise RuntimeError("V2-E.0.2 没有可用的 committed ASR token")
    disagreements = database.list_asr_disagreements(int(asr_run["id"]))
    episodes, excluded = build_episode_evidence(
        database,
        session_id=session_id,
        diarization_run_id=resolved_diarization_id,
        tokens=tokens,
        disagreement_rows=disagreements,
        settings=settings,
    )
    provider_request = build_provider_request(
        session,
        episodes,
        settings=settings,
    )
    _validate_provider_privacy(provider_request)
    return PreparedSemanticInput(
        recording_id=recording_id,
        session_id=session_id,
        duration_ms=int(session["duration_ms"]),
        asr_run_id=int(asr_run["id"]),
        diarization_run_id=resolved_diarization_id,
        tokens=tokens,
        episodes=episodes,
        excluded_blocks=excluded,
        provider_request=provider_request,
    )


def export_manual_semantic_bundle(
    database: Database,
    recording_id: int | None,
    output_path: Path,
    *,
    session_id: int | None = None,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
    settings: SemanticV2E02Settings | None = None,
) -> dict[str, Any]:
    prepared = prepare_semantic_v2e02(
        database,
        recording_id,
        session_id=session_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        settings=settings,
    )
    payload = {
        "format": MANUAL_BUNDLE_FORMAT,
        "provider_request_sha256": _sha256_json(prepared.provider_request),
        "provider_request": prepared.provider_request,
        "instructions": {
            "response_format": SEMANTIC_RESPONSE_FORMAT,
            "save_response_separately": True,
            "project_runtime_api_called": False,
            "manual_evaluator_must_not_claim_reproducible_model_version": True,
        },
    }
    _validate_provider_privacy(payload)
    _write_json(output_path, payload)
    return payload


def run_semantic_v2e02(
    database: Database,
    recording_id: int | None,
    *,
    session_id: int | None = None,
    asr_run_id: int | None = None,
    diarization_run_id: int | None = None,
    settings: SemanticV2E02Settings | None = None,
    provider: SemanticProvider | None = None,
) -> SemanticV2E02Summary:
    settings = settings or SemanticV2E02Settings()
    settings.validate()
    provider = provider or EpisodeContractMockProvider()
    if provider.network_access:
        raise ValueError("V2-E.0.2 本地阶段只允许不联网 provider")
    prepared = prepare_semantic_v2e02(
        database,
        recording_id,
        session_id=session_id,
        asr_run_id=asr_run_id,
        diarization_run_id=diarization_run_id,
        settings=settings,
    )
    ledger = build_evidence_ledger(database, prepared, settings=settings)
    request_sha256 = _sha256_json(ledger)
    provider_request_sha256 = _sha256_json(prepared.provider_request)
    config = {
        "evidence_format": EVIDENCE_LEDGER_FORMAT,
        "provider_request_format": LLM_TRANSPORT_FORMAT,
        "response_format": SEMANTIC_RESPONSE_FORMAT,
        "asr_run_id": prepared.asr_run_id,
        "diarization_run_id": prepared.diarization_run_id,
        "provider": provider.provider_name,
        "model": provider.model_name,
        "manual_eval": provider.manual_eval,
        **asdict(settings),
        "request_sha256": request_sha256,
        "provider_request_sha256": provider_request_sha256,
        "privacy": {
            "project_runtime_network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_receives_source_hashes": False,
            "provider_receives_token_database_ids": False,
            "manual_evaluator_external_to_runtime": provider.manual_eval,
        },
    }
    run_id = database.start_processing_run(
        recording_id,
        session_id=prepared.session_id,
        run_kind="semantic_v2e0",
        config=config,
        config_sha256=_sha256_json(config),
        model_manifest={
            "provider": provider.provider_name,
            "model": provider.model_name,
            "project_runtime_network_access": False,
            "manual_eval": provider.manual_eval,
            "audio_uploaded": False,
            "provider_payload_sha256": provider_request_sha256,
        },
        pipeline_version="v2-e.0.2",
        parent_run_id=(
            prepared.diarization_run_id or prepared.asr_run_id
        ),
    )
    try:
        response = provider.generate(prepared.provider_request)
        validate_semantic_response(
            response,
            prepared.episodes,
            provider_request_sha256=provider_request_sha256,
        )
        response_sha256 = _sha256_json(response)
        candidates = response_candidates(
            run_id,
            response,
            prepared.episodes,
            review_clip_ms=settings.review_clip_ms,
        )
        database.create_semantic_snapshot(
            run_id,
            {
                "asr_run_id": prepared.asr_run_id,
                "diarization_run_id": prepared.diarization_run_id,
                "provider": provider.provider_name,
                "model": provider.model_name,
                "request_format": EVIDENCE_LEDGER_FORMAT,
                "response_format": SEMANTIC_RESPONSE_FORMAT,
                "request": ledger,
                "response": response,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "audio_bytes_included": False,
                "source_paths_included": False,
            },
            candidates,
        )
        summary_payload = {
            "session_id": prepared.session_id,
            "asr_run_id": prepared.asr_run_id,
            "diarization_run_id": prepared.diarization_run_id,
            "provider": provider.provider_name,
            "model": provider.model_name,
            "mode": str(response.get("mode") or "unknown"),
            "manual_eval": provider.manual_eval,
            "episode_count": len(prepared.episodes),
            "excluded_block_count": len(prepared.excluded_blocks),
            "llm_job_count": len(prepared.provider_request["jobs"]),
            "llm_payload_bytes": len(
                _canonical_json(prepared.provider_request).encode("utf-8")
            ),
            "token_count": len(prepared.tokens),
            "candidate_count": len(candidates),
            "scene_count": len(response["scenes"]),
            "claim_count": len(response["claims"]),
            "action_count": len(response["actions"]),
            "unresolved_count": len(response["unresolved"]),
            "request_sha256": request_sha256,
            "provider_request_sha256": provider_request_sha256,
            "response_sha256": response_sha256,
            "privacy": config["privacy"],
        }
        manifest_path = _write_manifest(
            run_id,
            prepared.session_id,
            config,
            ledger,
            prepared.provider_request,
            response,
            summary_payload,
        )
        database.finish_processing_run(
            run_id,
            status="completed",
            summary=summary_payload,
            artifacts={"manifest": str(manifest_path.resolve())},
        )
        return SemanticV2E02Summary(
            run_id=run_id,
            asr_run_id=prepared.asr_run_id,
            diarization_run_id=prepared.diarization_run_id,
            episode_count=len(prepared.episodes),
            excluded_block_count=len(prepared.excluded_blocks),
            llm_job_count=len(prepared.provider_request["jobs"]),
            llm_payload_bytes=summary_payload["llm_payload_bytes"],
            token_count=len(prepared.tokens),
            candidate_count=len(candidates),
            scene_count=len(response["scenes"]),
            claim_count=len(response["claims"]),
            action_count=len(response["actions"]),
            unresolved_count=len(response["unresolved"]),
            request_sha256=request_sha256,
            provider_request_sha256=provider_request_sha256,
            response_sha256=response_sha256,
            manifest_path=manifest_path,
        )
    except Exception as exc:
        database.finish_processing_run(run_id, status="failed", error=repr(exc))
        raise


def build_episode_evidence(
    database: Database,
    *,
    session_id: int,
    diarization_run_id: int | None,
    tokens: Sequence[dict[str, Any]],
    disagreement_rows: Sequence[Any],
    settings: SemanticV2E02Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_settings = SemanticV2E01Settings(
        conversation_gap_ms=settings.episode_gap_ms,
        utterance_gap_ms=settings.utterance_gap_ms,
        min_informative_chars=settings.min_informative_chars,
        max_llm_request_chars=settings.max_llm_request_chars,
        chunk_overlap_utterances=settings.chunk_overlap_utterances,
        review_clip_ms=settings.review_clip_ms,
        transcript_preview_chars=settings.transcript_preview_chars,
    )
    base_episodes, excluded = build_conversation_evidence(
        tokens,
        disagreement_rows,
        settings=base_settings,
    )
    source_regions, identity_regions = _evidence_regions(database, session_id)
    contaminated = _contaminated_voice_clusters(
        database, diarization_run_id
    )
    episodes: list[dict[str, Any]] = []
    for index, base in enumerate(base_episodes, start=1):
        key = f"episode-{index:04d}"
        utterances = []
        for utterance_index, item in enumerate(base["utterances"], start=1):
            start_ms = int(item["start_ms"])
            end_ms = int(item["end_ms"])
            speaker = str(item["speaker"])
            source = _resolve_interval_evidence(
                start_ms,
                end_ms,
                source_regions,
                settings=settings,
                unknown_label="unknown",
            )
            identity = _resolve_interval_evidence(
                start_ms,
                end_ms,
                identity_regions,
                settings=settings,
                unknown_label="unknown",
            )
            alternative_available = any(
                int(value["end_ms"]) > start_ms
                and int(value["start_ms"]) < end_ms
                for value in base["asr_alternatives"]
            )
            utterances.append(
                {
                    **item,
                    "key": f"{key}:u-{utterance_index:04d}",
                    "voice_cluster": {
                        "label": None if speaker == "unassigned" else speaker,
                        "assignment": str(item["speaker_kind"]),
                        "identity_safe": False,
                        "contaminated": speaker in contaminated,
                    },
                    "source": source,
                    "identity": identity,
                    "semantic_uncertainty": {
                        "speaker_uncertain": (
                            str(item["speaker_kind"]) != "primary"
                        ),
                        "overlap": bool(
                            item["uncertainty"]["overlap_tokens"]
                        ),
                        "asr_alternative_available": alternative_available,
                    },
                }
            )
        episodes.append(
            {
                **base,
                "key": key,
                "semantic_role": "context_container_only",
                "boundary": {
                    **base["boundary"],
                    "policy": "token-gap-episode-candidate-v2",
                    "episode_gap_ms": settings.episode_gap_ms,
                    "hard_duration_cap_ms": None,
                    "is_semantic_scene": False,
                },
                "voice_clusters": [
                    {
                        **value,
                        "identity_safe": False,
                        "contaminated": str(value["label"]) in contaminated,
                    }
                    for value in base["speakers"]
                ],
                "utterances": utterances,
            }
        )
    for value in excluded:
        value["boundary"] = {
            **value["boundary"],
            "policy": "token-gap-episode-candidate-v2",
            "episode_gap_ms": settings.episode_gap_ms,
            "is_semantic_scene": False,
        }
    return episodes, excluded


def build_provider_request(
    session: Any,
    episodes: Sequence[dict[str, Any]],
    *,
    settings: SemanticV2E02Settings,
) -> dict[str, Any]:
    compact = [_compact_episode(item) for item in episodes]
    jobs = _pack_jobs(compact, settings=settings)
    request = {
        "format": LLM_TRANSPORT_FORMAT,
        "strategy": "episode-utterance-four-track-evidence-v2",
        "session": {
            "recorded_at": str(session["recorded_at"]),
            "timezone": str(session["timezone"]),
            "duration_ms": int(session["duration_ms"]),
        },
        "policy": {
            "episode_is_context_container_not_scene": True,
            "hard_episode_duration_cap_ms": None,
            "utterance_is_only_primary_text": True,
            "voice_cluster_is_identity": False,
            "source_and_identity_are_interval_evidence": True,
            "split_only_for_provider_context_limit": True,
            "chunk_overlap_utterances": settings.chunk_overlap_utterances,
            "review_clip_is_semantic_boundary": False,
            "source_audio_available_to_provider": False,
        },
        "jobs": jobs,
    }
    _validate_provider_privacy(request)
    return request


def build_evidence_ledger(
    database: Database,
    prepared: PreparedSemanticInput,
    *,
    settings: SemanticV2E02Settings,
) -> dict[str, Any]:
    session = database.get_recording_session(prepared.session_id)
    return {
        "format": EVIDENCE_LEDGER_FORMAT,
        "mode": "local_audit_ledger_plus_episode_provider_payload",
        "session": {
            "session_id": prepared.session_id,
            "recorded_at": str(session["recorded_at"]),
            "timezone": str(session["timezone"]),
            "duration_ms": int(session["duration_ms"]),
            "device": session["device"],
        },
        "inputs": {
            "asr_run_id": prepared.asr_run_id,
            "diarization_run_id": prepared.diarization_run_id,
            "input_fingerprint": database.session_input_fingerprint(
                prepared.session_id
            ),
        },
        "entity_contract": {
            "episode": "context_container_not_semantic_scene",
            "utterance": "only_provider_primary_text_unit",
            "voice_cluster": "anonymous_soft_evidence_never_identity",
            "source": "interval_evidence",
            "identity": "interval_evidence",
            "llm_outputs": ["scene", "claim", "action", "unresolved"],
        },
        "boundary_contract": {
            "episode_gap_ms": settings.episode_gap_ms,
            "hard_episode_duration_cap_ms": None,
            "review_clip_ms": settings.review_clip_ms,
            "review_clip_is_semantic_boundary": False,
            "provider_chunk_is_semantic_boundary": False,
        },
        "privacy": {
            "project_runtime_network_access": False,
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_payload_contains_source_hashes": False,
            "provider_payload_contains_database_token_ids": False,
            "local_ledger_uses_source_hashes_for_audit": True,
        },
        "evidence_ledger": {
            "episodes": prepared.episodes,
            "excluded_blocks": prepared.excluded_blocks,
        },
        "llm_transport": prepared.provider_request,
        "output_contract": {
            "required": [
                "daily_summary",
                "scenes",
                "claims",
                "actions",
                "unresolved",
            ],
            "claims_must_cite_utterance_ids": True,
            "specific_subject_requires_identity_evidence": True,
            "media_only_evidence_cannot_be_personal_fact": True,
            "may_overwrite_asr": False,
            "external_write_requires_human_confirmation": True,
        },
    }


def validate_semantic_response(
    response: dict[str, Any],
    episodes: Sequence[dict[str, Any]],
    *,
    provider_request_sha256: str,
) -> None:
    if str(response.get("format")) != SEMANTIC_RESPONSE_FORMAT:
        raise ValueError("语义响应格式不受支持")
    if str(response.get("request_sha256")) != provider_request_sha256:
        raise ValueError("语义响应没有绑定当前 provider request")
    for key in ("daily_summary", "scenes", "claims", "actions", "unresolved"):
        if key not in response:
            raise TypeError(f"语义响应缺少 {key}")
    if not all(
        isinstance(response[key], list)
        for key in ("scenes", "claims", "actions", "unresolved")
    ):
        raise TypeError("语义响应列表字段格式无效")
    summary = response["daily_summary"]
    if not isinstance(summary, dict):
        raise TypeError("daily_summary 格式无效")
    _validate_title_body(summary, label="daily_summary")

    episode_by_key = {str(value["key"]): value for value in episodes}
    utterance_by_key: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for episode in episodes:
        for utterance in episode["utterances"]:
            utterance_by_key[str(utterance["key"])] = (episode, utterance)

    scene_by_key: dict[str, dict[str, Any]] = {}
    for scene in response["scenes"]:
        key = _unique_response_key(scene, scene_by_key, label="scene")
        episode_key = str(scene.get("episode_id") or "")
        if episode_key not in episode_by_key:
            raise ValueError(f"scene {key} 引用了不存在的 episode")
        episode = episode_by_key[episode_key]
        start_ms = int(scene.get("start_ms", -1))
        end_ms = int(scene.get("end_ms", -1))
        if (
            start_ms < int(episode["start_ms"])
            or end_ms > int(episode["end_ms"])
            or end_ms <= start_ms
        ):
            raise ValueError(f"scene {key} 超出 episode 范围")
        _validate_title_body(scene, label=f"scene {key}", body_key="summary")
        evidence = _evidence_ids(scene)
        _validate_evidence_ids(
            evidence,
            utterance_by_key,
            episode_key=episode_key,
            start_ms=start_ms,
            end_ms=end_ms,
            label=f"scene {key}",
        )
        for participant in scene.get("participants") or []:
            participant = str(participant)
            if participant in {"unknown", "media", "mixed", ""}:
                continue
            if not _identity_supported(participant, evidence, utterance_by_key):
                raise ValueError(
                    f"scene {key} 的人物 {participant} 没有区间身份依据"
                )
        scene_by_key[key] = scene

    summary_scene_ids = list(summary.get("evidence_scene_ids") or [])
    if any(str(key) not in scene_by_key for key in summary_scene_ids):
        raise ValueError("daily_summary 引用了不存在的 scene")

    seen_claims: dict[str, dict[str, Any]] = {}
    for claim in response["claims"]:
        key = _unique_response_key(claim, seen_claims, label="claim")
        scene_key = str(claim.get("scene_id") or "")
        if scene_key not in scene_by_key:
            raise ValueError(f"claim {key} 引用了不存在的 scene")
        text = str(claim.get("text") or "").strip()
        if not text:
            raise ValueError(f"claim {key} 缺少 text")
        evidence = _evidence_ids(claim)
        _validate_evidence_ids(
            evidence,
            utterance_by_key,
            episode_key=str(scene_by_key[scene_key]["episode_id"]),
            label=f"claim {key}",
        )
        subject = str(claim.get("subject") or "unknown")
        if subject not in {"unknown", "media", "mixed"} and not _identity_supported(
            subject, evidence, utterance_by_key
        ):
            raise ValueError(f"claim {key} 的主体没有区间身份依据")
        if str(claim.get("claim_type")) != "media_content" and _all_media(
            evidence, utterance_by_key
        ):
            raise ValueError(f"claim {key} 把纯媒体证据写成了个人事实")
        seen_claims[key] = claim

    seen_actions: dict[str, dict[str, Any]] = {}
    for action in response["actions"]:
        key = _unique_response_key(action, seen_actions, label="action")
        scene_key = str(action.get("scene_id") or "")
        if scene_key not in scene_by_key:
            raise ValueError(f"action {key} 引用了不存在的 scene")
        text = str(action.get("text") or "").strip()
        if not text:
            raise ValueError(f"action {key} 缺少 text")
        if action.get("requires_human_confirmation") is not True:
            raise ValueError(f"action {key} 必须要求人工确认")
        evidence = _evidence_ids(action)
        _validate_evidence_ids(
            evidence,
            utterance_by_key,
            episode_key=str(scene_by_key[scene_key]["episode_id"]),
            label=f"action {key}",
        )
        owner = str(action.get("owner") or "unknown")
        if owner not in {"unknown", "mixed"} and not _identity_supported(
            owner, evidence, utterance_by_key
        ):
            raise ValueError(f"action {key} 的负责人没有区间身份依据")
        if _all_media(evidence, utterance_by_key):
            raise ValueError(f"action {key} 只引用了媒体内容")
        seen_actions[key] = action

    unresolved_seen: dict[str, dict[str, Any]] = {}
    for item in response["unresolved"]:
        key = _unique_response_key(item, unresolved_seen, label="unresolved")
        description = str(item.get("description") or "").strip()
        if not description:
            raise ValueError(f"unresolved {key} 缺少 description")
        episode_key = str(item.get("episode_id") or "")
        if episode_key and episode_key not in episode_by_key:
            raise ValueError(f"unresolved {key} 引用了不存在的 episode")
        evidence = _evidence_ids(item)
        _validate_evidence_ids(
            evidence,
            utterance_by_key,
            episode_key=episode_key or None,
            label=f"unresolved {key}",
        )
        unresolved_seen[key] = item


def response_candidates(
    run_id: int,
    response: dict[str, Any],
    episodes: Sequence[dict[str, Any]],
    *,
    review_clip_ms: int,
) -> list[dict[str, Any]]:
    utterance_by_key = {
        str(utterance["key"]): (episode, utterance)
        for episode in episodes
        for utterance in episode["utterances"]
    }
    scenes = list(response["scenes"])
    if scenes:
        summary_start = min(int(item["start_ms"]) for item in scenes)
        summary_end = max(int(item["end_ms"]) for item in scenes)
    elif episodes:
        summary_start = int(episodes[0]["start_ms"])
        summary_end = int(episodes[-1]["end_ms"])
    else:
        coverage = response.get("coverage") or {}
        summary_start = int(coverage.get("start_ms") or 0)
        summary_end = max(summary_start + 1, int(coverage.get("end_ms") or 1))
    summary = response["daily_summary"]
    candidates = [
        {
            "candidate_key": f"semantic:{run_id}:daily-summary",
            "candidate_type": "daily_summary",
            "session_start_ms": summary_start,
            "session_end_ms": summary_end,
            "title": str(summary["title"]),
            "body": str(summary["body"]),
            "confidence": None,
            "evidence": {
                "semantic_unit": "day",
                "scene_ids": list(summary.get("evidence_scene_ids") or []),
                "manual_eval": str(response.get("mode")) == "codex_manual_eval",
                "semantic_inference_performed": (
                    str(response.get("mode")) != "contract_mock_no_semantic_inference"
                ),
            },
        }
    ]
    for scene in scenes:
        evidence_ids = _evidence_ids(scene)
        evidence = _candidate_evidence(
            evidence_ids,
            utterance_by_key,
            review_clip_ms=review_clip_ms,
        )
        candidates.append(
            {
                "candidate_key": f"semantic:{run_id}:{scene['id']}",
                "candidate_type": "event",
                "session_start_ms": int(scene["start_ms"]),
                "session_end_ms": int(scene["end_ms"]),
                "title": str(scene["title"]),
                "body": str(scene["summary"]),
                "confidence": scene.get("confidence"),
                "evidence": {
                    **evidence,
                    "semantic_unit": "scene",
                    "scene_id": str(scene["id"]),
                    "episode_id": str(scene["episode_id"]),
                    "scene_type": str(scene.get("type") or "unknown"),
                    "participants": list(scene.get("participants") or []),
                },
            }
        )
    scene_by_key = {str(value["id"]): value for value in scenes}
    for claim in response["claims"]:
        evidence_ids = _evidence_ids(claim)
        evidence = _candidate_evidence(
            evidence_ids,
            utterance_by_key,
            review_clip_ms=review_clip_ms,
        )
        scene = scene_by_key[str(claim["scene_id"])]
        candidates.append(
            {
                "candidate_key": f"semantic:{run_id}:{claim['id']}",
                "candidate_type": "fact",
                "session_start_ms": min(
                    int(utterance_by_key[key][1]["start_ms"])
                    for key in evidence_ids
                ),
                "session_end_ms": max(
                    int(utterance_by_key[key][1]["end_ms"])
                    for key in evidence_ids
                ),
                "title": f"陈述 · {claim.get('subject') or 'unknown'}",
                "body": str(claim["text"]),
                "confidence": claim.get("confidence"),
                "evidence": {
                    **evidence,
                    "semantic_unit": "claim",
                    "claim_id": str(claim["id"]),
                    "claim_type": str(claim.get("claim_type") or "unknown"),
                    "subject": str(claim.get("subject") or "unknown"),
                    "scene_id": str(scene["id"]),
                    "episode_id": str(scene["episode_id"]),
                },
            }
        )
    for action in response["actions"]:
        evidence_ids = _evidence_ids(action)
        evidence = _candidate_evidence(
            evidence_ids,
            utterance_by_key,
            review_clip_ms=review_clip_ms,
        )
        scene = scene_by_key[str(action["scene_id"])]
        candidates.append(
            {
                "candidate_key": f"semantic:{run_id}:{action['id']}",
                "candidate_type": "action",
                "session_start_ms": min(
                    int(utterance_by_key[key][1]["start_ms"])
                    for key in evidence_ids
                ),
                "session_end_ms": max(
                    int(utterance_by_key[key][1]["end_ms"])
                    for key in evidence_ids
                ),
                "title": f"行动候选 · {action.get('owner') or 'unknown'}",
                "body": str(action["text"]),
                "confidence": action.get("confidence"),
                "evidence": {
                    **evidence,
                    "semantic_unit": "action",
                    "action_id": str(action["id"]),
                    "owner": str(action.get("owner") or "unknown"),
                    "due": action.get("due"),
                    "requires_human_confirmation": True,
                    "scene_id": str(scene["id"]),
                    "episode_id": str(scene["episode_id"]),
                },
            }
        )
    return candidates


def semantic_overview(
    database: Database,
    recording_id: int | None = None,
    *,
    session_id: int | None = None,
) -> dict[str, Any]:
    recording_id, session_id = _resolve_overview_target(
        database, recording_id, session_id
    )
    completed = [
        row
        for row in database.list_session_processing_runs(session_id)
        if str(row["run_kind"]) == "semantic_v2e0"
        and str(row["status"]) == "completed"
    ]
    if not completed:
        if recording_id is not None:
            return v2e01_semantic_overview(database, recording_id)
        return _empty_session_semantic_overview(database, session_id)
    run = completed[-1]
    exchange = database.get_semantic_exchange(int(run["id"]))
    if str(exchange["request_format"]) != EVIDENCE_LEDGER_FORMAT:
        if recording_id is not None:
            return v2e01_semantic_overview(database, recording_id)
        return _empty_session_semantic_overview(
            database,
            session_id,
            reason="最新语义结果使用旧版格式，原生会话页面暂不展示。",
        )
    request = _json_object(exchange["request_json"])
    response = _json_object(exchange["response_json"])
    ledger = request.get("evidence_ledger") or {}
    episodes = list(ledger.get("episodes") or [])
    candidates: list[dict[str, Any]] = []
    reviewed = 0
    for row in database.list_semantic_candidates(int(run["id"])):
        revisions = database.list_semantic_candidate_revisions(int(row["id"]))
        latest = revisions[-1] if revisions else None
        if latest is not None:
            reviewed += 1
        evidence = _json_object(row["evidence_json"])
        review_clips = [
            {
                **clip,
                "audio_url": _semantic_audio_url(
                    recording_id,
                    session_id,
                    start_ms=int(clip["start_ms"]),
                    end_ms=int(clip["end_ms"]),
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
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "title": str(latest["title"] if latest else row["title"]),
                "body": str(latest["body"] if latest else row["body"]),
                "base_title": str(row["title"]),
                "base_body": str(row["body"]),
                "review_status": str(latest["status"]) if latest else None,
                "revision_index": int(latest["revision_index"]) if latest else 0,
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
    transport = request.get("llm_transport") or {}
    return {
        "available": True,
        "recording_id": recording_id,
        "session_id": session_id,
        "version": "v2-e.0.2",
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
            "manual_eval": bool(summary.get("manual_eval")),
            "request_sha256": str(exchange["request_sha256"]),
            "response_sha256": str(exchange["response_sha256"]),
        },
        "summary": summary,
        "privacy": dict(summary.get("privacy") or {}),
        "transport": {
            "strategy": transport.get("strategy"),
            "job_count": len(transport.get("jobs") or []),
            "episode_is_context_container": True,
            "hard_episode_duration_cap_ms": None,
            "review_clip_ms": request.get("boundary_contract", {}).get(
                "review_clip_ms"
            ),
        },
        "episodes": [_episode_overview(item) for item in episodes],
        "excluded_blocks": [
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
        ],
        "unresolved": list(response.get("unresolved") or []),
        "reviewed_candidates": reviewed,
        "candidates": candidates,
    }


def _resolve_overview_target(
    database: Database,
    recording_id: int | None,
    session_id: int | None,
) -> tuple[int | None, int]:
    if session_id is None:
        if recording_id is None:
            raise ValueError("必须指定 recording_id 或 session_id")
        database.get_recording(recording_id)
        return recording_id, int(database.get_session_for_recording(recording_id)["id"])
    session = database.get_recording_session(session_id)
    legacy_recording_id = (
        int(session["legacy_recording_id"])
        if session["legacy_recording_id"] is not None
        else None
    )
    if recording_id is not None and recording_id != legacy_recording_id:
        raise ValueError("recording_id 与 session_id 不属于同一录音会话")
    return legacy_recording_id, session_id


def _empty_session_semantic_overview(
    database: Database,
    session_id: int,
    *,
    reason: str = "还没有 V2-E.0.2 本地语义证据包。",
) -> dict[str, Any]:
    can_generate = True
    inputs: dict[str, int | None] | None = None
    try:
        asr_run, diarization_run = resolve_semantic_input_runs(
            database, None, session_id=session_id
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
    return {
        "available": False,
        "recording_id": None,
        "session_id": session_id,
        "can_generate": can_generate,
        "reason": reason,
        "inputs": inputs,
    }


def _semantic_audio_url(
    recording_id: int | None,
    session_id: int,
    *,
    start_ms: int,
    end_ms: int,
) -> str:
    target = (
        f"recording_id={recording_id}"
        if recording_id is not None
        else f"session_id={session_id}"
    )
    return (
        "/api/speaker-timeline/audio"
        f"?{target}&start_ms={start_ms}&end_ms={end_ms}&v=2"
    )


def assemble_transport_episodes(request: dict[str, Any]) -> list[dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    seen_utterances: dict[str, set[str]] = defaultdict(set)
    seen_alternatives: dict[str, set[str]] = defaultdict(set)
    for job in request.get("jobs") or []:
        for episode in job.get("episodes") or []:
            key = str(episode["key"])
            if key not in by_key:
                value = dict(episode)
                value["start_ms"] = int(
                    episode.get("parent_start_ms", episode["start_ms"])
                )
                value["end_ms"] = int(
                    episode.get("parent_end_ms", episode["end_ms"])
                )
                value["complete_episode"] = True
                value["utterances"] = []
                value["asr_alternatives"] = []
                by_key[key] = value
            value = by_key[key]
            for utterance in episode.get("utterances") or []:
                utterance_key = str(utterance["id"])
                if utterance_key in seen_utterances[key]:
                    continue
                seen_utterances[key].add(utterance_key)
                value["utterances"].append(dict(utterance))
            for alternative in episode.get("asr_alternatives") or []:
                alternative_key = str(alternative["key"])
                if alternative_key in seen_alternatives[key]:
                    continue
                seen_alternatives[key].add(alternative_key)
                value["asr_alternatives"].append(dict(alternative))
    output = list(by_key.values())
    for value in output:
        value["utterances"].sort(key=lambda item: (item["start_ms"], item["id"]))
        value["asr_alternatives"].sort(
            key=lambda item: (item["start_ms"], item["key"])
        )
    return sorted(output, key=lambda item: (item["start_ms"], item["key"]))


def _evidence_regions(
    database: Database, session_id: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_by_key: dict[tuple[int, int, str], dict[str, Any]] = {}
    identity_by_key: dict[tuple[int, int, str], dict[str, Any]] = {}
    for truth_set in database.list_truth_sets(session_id):
        if str(truth_set["status"]) != "frozen":
            continue
        for row in database.list_truth_annotations(int(truth_set["id"])):
            start_ms = int(row["session_start_ms"])
            end_ms = int(row["session_end_ms"])
            metadata = _json_object(row["metadata_json"])
            kind = str(row["annotation_kind"])
            label = str(row["label"] or "").strip()
            provenance = "human_truth_interval"
            if kind == "speech":
                source = str(metadata.get("speech_source") or "")
                if source in SOURCE_LABELS - {"unknown"}:
                    source_by_key[(start_ms, end_ms, source)] = {
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "label": source,
                        "provenance": provenance,
                        "local_evidence": {
                            "truth_set_id": int(truth_set["id"]),
                            "annotation_key": str(row["annotation_key"]),
                        },
                    }
            if kind != "speaker":
                continue
            normalized = label.lower()
            if normalized in {"tv", "media", "media_playback"}:
                source_by_key[(start_ms, end_ms, "media_playback")] = {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "label": "media_playback",
                    "provenance": "legacy_human_speaker_media_interval",
                    "local_evidence": {
                        "truth_set_id": int(truth_set["id"]),
                        "annotation_key": str(row["annotation_key"]),
                    },
                }
            elif normalized not in NON_IDENTITY_LABELS:
                identity_by_key[(start_ms, end_ms, label)] = {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "label": label,
                    "provenance": provenance,
                    "local_evidence": {
                        "truth_set_id": int(truth_set["id"]),
                        "annotation_key": str(row["annotation_key"]),
                    },
                }
                source_by_key[(start_ms, end_ms, "live_person")] = {
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "label": "live_person",
                    "provenance": "human_identity_implies_live_person",
                    "local_evidence": {
                        "truth_set_id": int(truth_set["id"]),
                        "annotation_key": str(row["annotation_key"]),
                    },
                }
    for row in database.list_identity_reference_intervals():
        if (
            int(row["session_id"]) != session_id
            or str(row["decision"]) != "confirmed_target"
        ):
            continue
        label = str(row["identity_label"]).strip()
        if label.lower() in NON_IDENTITY_LABELS:
            continue
        start_ms = int(row["session_start_ms"])
        end_ms = int(row["session_end_ms"])
        key = (start_ms, end_ms, label)
        if key not in identity_by_key:
            identity_by_key[key] = {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "label": label,
                "provenance": "reviewed_identity_reference",
                "local_evidence": {
                    "reference_id": int(row["id"]),
                    "provenance_kind": str(row["provenance_kind"]),
                },
            }
        source_by_key[(start_ms, end_ms, "live_person")] = {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "label": "live_person",
            "provenance": "reviewed_identity_implies_live_person",
            "local_evidence": {
                "reference_id": int(row["id"]),
                "provenance_kind": str(row["provenance_kind"]),
            },
        }
    return list(source_by_key.values()), list(identity_by_key.values())


def _contaminated_voice_clusters(
    database: Database, diarization_run_id: int | None
) -> set[str]:
    if diarization_run_id is None:
        return set()
    matches = []
    run = database.get_processing_run(diarization_run_id)
    for row in database.list_session_processing_runs(int(run["session_id"])):
        if (
            str(row["run_kind"]) != "quality_diarization_v2d2"
            or str(row["status"]) != "completed"
        ):
            continue
        summary = _json_object(row["summary_json"])
        if int(summary.get("diarization_run_id") or 0) == diarization_run_id:
            matches.append(summary)
    if not matches:
        return set()
    return {str(value) for value in matches[-1].get("contaminated_speakers", [])}


def _resolve_interval_evidence(
    start_ms: int,
    end_ms: int,
    regions: Sequence[dict[str, Any]],
    *,
    settings: SemanticV2E02Settings,
    unknown_label: str,
) -> dict[str, Any]:
    duration_ms = max(1, end_ms - start_ms)
    ranges_by_label: dict[str, list[tuple[int, int]]] = defaultdict(list)
    provenance_by_label: dict[str, set[str]] = defaultdict(set)
    local_by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for region in regions:
        overlap_start = max(start_ms, int(region["start_ms"]))
        overlap_end = min(end_ms, int(region["end_ms"]))
        if overlap_end <= overlap_start:
            continue
        label = str(region["label"])
        ranges_by_label[label].append((overlap_start, overlap_end))
        provenance_by_label[label].add(str(region["provenance"]))
        local_by_label[label].append(dict(region.get("local_evidence") or {}))
    evidence = []
    for label, ranges in ranges_by_label.items():
        overlap_ms = sum(end - start for start, end in _merge_ranges(ranges))
        evidence.append(
            {
                "label": label,
                "overlap_ms": overlap_ms,
                "overlap_ratio": overlap_ms / duration_ms,
                "provenance": sorted(provenance_by_label[label]),
                "local_evidence": local_by_label[label],
            }
        )
    evidence.sort(key=lambda item: (-item["overlap_ratio"], item["label"]))
    if not evidence:
        return {"label": unknown_label, "resolution": "none", "evidence": []}
    first = evidence[0]
    second_ratio = evidence[1]["overlap_ratio"] if len(evidence) > 1 else 0.0
    resolved = (
        first["overlap_ratio"] >= settings.evidence_resolution_min_ratio
        and first["overlap_ratio"] - second_ratio
        >= settings.evidence_resolution_min_margin
    )
    return {
        "label": str(first["label"]) if resolved else unknown_label,
        "resolution": "human_interval" if resolved else "ambiguous_intervals",
        "evidence": evidence,
    }


def _compact_episode(episode: dict[str, Any]) -> dict[str, Any]:
    contaminated = {
        str(value["label"]): bool(value["contaminated"])
        for value in episode["voice_clusters"]
    }
    return {
        "key": str(episode["key"]),
        "start_ms": int(episode["start_ms"]),
        "end_ms": int(episode["end_ms"]),
        "complete_episode": True,
        "semantic_role": "context_container_only",
        "voice_clusters": [
            {
                "label": str(value["label"]),
                "identity_safe": False,
                "contaminated": bool(value["contaminated"]),
            }
            for value in episode["voice_clusters"]
        ],
        "utterances": [
            {
                "id": str(item["key"]),
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
                "text": str(item["text"]),
                "voice_cluster": {
                    "label": item["voice_cluster"]["label"],
                    "assignment": str(item["voice_cluster"]["assignment"]),
                    "identity_safe": False,
                    "contaminated": bool(
                        contaminated.get(
                            str(item["voice_cluster"]["label"]), False
                        )
                    ),
                },
                "source": _compact_resolved_evidence(item["source"]),
                "identity": _compact_resolved_evidence(item["identity"]),
                "uncertainty": dict(item["semantic_uncertainty"]),
            }
            for item in episode["utterances"]
        ],
        "asr_alternatives": [
            {
                "key": str(item["key"]),
                "start_ms": int(item["start_ms"]),
                "end_ms": int(item["end_ms"]),
                "priority": str(item["priority"]),
                "normalized_distance": float(item["normalized_distance"]),
                "secondary_text": str(item["secondary_text"]),
                "scope": "alternative_for_same_time_range_not_extra_dialogue",
            }
            for item in episode["asr_alternatives"]
        ],
    }


def _compact_resolved_evidence(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "label": str(value["label"]),
        "resolution": str(value["resolution"]),
        "evidence": [
            {
                "label": str(item["label"]),
                "overlap_ratio": round(float(item["overlap_ratio"]), 6),
                "provenance": list(item["provenance"]),
            }
            for item in value["evidence"]
        ],
    }


def _pack_jobs(
    episodes: Sequence[dict[str, Any]],
    *,
    settings: SemanticV2E02Settings,
) -> list[dict[str, Any]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for episode in episodes:
        episode_chars = len(_canonical_json(episode))
        if episode_chars > settings.max_llm_request_chars:
            if current:
                batches.append(current)
                current = []
                current_chars = 0
            batches.extend(
                [item] for item in _split_large_episode(episode, settings=settings)
            )
            continue
        if current and current_chars + episode_chars > settings.max_llm_request_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(episode)
        current_chars += episode_chars
    if current:
        batches.append(current)
    return [
        {
            "format": LLM_REQUEST_FORMAT,
            "job_key": f"semantic-job-{index:04d}",
            "task": "extract_evidence_grounded_scenes_claims_and_actions",
            "instructions": [
                "An episode is a context container, never a semantic scene.",
                "Use utterances as the only primary transcript evidence.",
                "A voice_cluster is anonymous soft evidence and never identity.",
                "Keep live_person, media_playback, and identity evidence separate.",
                "Never turn media-only content into personal experience or action.",
                "Specific people require interval identity evidence.",
                "Every scene, claim, action, and unresolved item must cite utterance ids.",
                "Preserve uncertainty and never overwrite ASR evidence.",
            ],
            "episodes": batch,
            "response_contract": _provider_response_contract(),
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


def _split_large_episode(
    episode: dict[str, Any],
    *,
    settings: SemanticV2E02Settings,
) -> list[dict[str, Any]]:
    utterances = list(episode["utterances"])
    if not utterances:
        return [episode]
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
    values = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_start = int(chunk[0]["start_ms"])
        chunk_end = int(chunk[-1]["end_ms"])
        values.append(
            {
                **episode,
                "start_ms": chunk_start,
                "end_ms": chunk_end,
                "complete_episode": False,
                "parent_start_ms": int(episode["start_ms"]),
                "parent_end_ms": int(episode["end_ms"]),
                "chunk_index": index,
                "chunk_count": len(chunks),
                "utterances": chunk,
                "asr_alternatives": [
                    item
                    for item in episode["asr_alternatives"]
                    if int(item["end_ms"]) > chunk_start
                    and int(item["start_ms"]) < chunk_end
                ],
                "context_policy": "utterance-boundary-with-leading-overlap",
            }
        )
    return values


def _provider_response_contract() -> dict[str, Any]:
    return {
        "format": SEMANTIC_RESPONSE_FORMAT,
        "top_level_fields": {
            "format": "exact response format string",
            "request_sha256": "SHA-256 supplied for the complete provider request",
            "mode": "provider-defined generation mode",
            "daily_summary": {
                "title": "string",
                "body": "string",
                "evidence_scene_ids": ["scene id"],
            },
            "scenes": [
                {
                    "id": "unique scene id",
                    "episode_id": "existing episode key",
                    "start_ms": "integer within episode",
                    "end_ms": "integer within episode and greater than start_ms",
                    "title": "string",
                    "summary": "string preserving uncertainty",
                    "type": "provider taxonomy string",
                    "participants": [
                        "identity label only when supported; otherwise unknown/media/mixed"
                    ],
                    "confidence": "number or null",
                    "evidence_utterance_ids": ["one or more utterance ids"],
                }
            ],
            "claims": [
                {
                    "id": "unique claim id",
                    "scene_id": "existing scene id",
                    "subject": "supported identity or unknown/media/mixed",
                    "claim_type": "media_content or a grounded non-media type",
                    "text": "string",
                    "confidence": "number or null",
                    "evidence_utterance_ids": ["one or more utterance ids"],
                }
            ],
            "actions": [
                {
                    "id": "unique action id",
                    "scene_id": "existing scene id",
                    "owner": "supported identity or unknown/mixed",
                    "text": "string",
                    "due": "string or null",
                    "confidence": "number or null",
                    "requires_human_confirmation": True,
                    "evidence_utterance_ids": ["one or more utterance ids"],
                }
            ],
            "unresolved": [
                {
                    "id": "unique unresolved id",
                    "episode_id": "existing episode key",
                    "description": "string",
                    "evidence_utterance_ids": ["one or more utterance ids"],
                }
            ],
            "coverage": {"start_ms": "integer", "end_ms": "integer"},
        },
        "rules": {
            "episode_is_not_scene": True,
            "evidence_ids_required": True,
            "scene_evidence_must_stay_inside_scene_time_range": True,
            "specific_subject_requires_identity_evidence": True,
            "media_only_evidence_cannot_form_personal_claim": True,
            "external_actions_require_human_confirmation": True,
            "empty_arrays_are_allowed": True,
        },
    }


def _candidate_evidence(
    evidence_ids: Sequence[str],
    utterance_by_key: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    review_clip_ms: int,
) -> dict[str, Any]:
    utterances = [utterance_by_key[key][1] for key in evidence_ids]
    token_ids = sorted(
        {
            int(token_id)
            for utterance in utterances
            for token_id in utterance["evidence"]["token_ids"]
        }
    )
    source_refs = _deduplicate_source_refs(
        ref
        for utterance in utterances
        for ref in utterance["evidence"]["source_refs"]
    )
    start_ms = min(int(item["start_ms"]) for item in utterances)
    end_ms = max(int(item["end_ms"]) for item in utterances)
    return {
        "utterance_ids": list(evidence_ids),
        "token_ids": token_ids,
        "source_refs": source_refs,
        "source_labels": sorted(
            {str(item["source"]["label"]) for item in utterances}
        ),
        "identity_labels": sorted(
            {
                str(item["identity"]["label"])
                for item in utterances
                if str(item["identity"]["label"]) != "unknown"
            }
        ),
        "review_clips": _review_clips(start_ms, end_ms, review_clip_ms),
    }


def _episode_overview(episode: dict[str, Any]) -> dict[str, Any]:
    source_counts = Counter(
        str(item["source"]["label"]) for item in episode["utterances"]
    )
    identity_counts = Counter(
        str(item["identity"]["label"])
        for item in episode["utterances"]
        if str(item["identity"]["label"]) != "unknown"
    )
    return {
        "key": str(episode["key"]),
        "start_ms": int(episode["start_ms"]),
        "end_ms": int(episode["end_ms"]),
        "duration_ms": int(episode["duration_ms"]),
        "semantic_role": "context_container_only",
        "utterance_count": len(episode["utterances"]),
        "transcript_preview": str(episode["transcript_preview"]),
        "source_counts": dict(source_counts),
        "identity_counts": dict(identity_counts),
        "voice_clusters": list(episode["voice_clusters"]),
        "uncertainty": dict(episode["uncertainty"]),
        "review_clips": list(episode["review_clips"]),
    }


def _unique_response_key(
    value: dict[str, Any],
    seen: dict[str, dict[str, Any]],
    *,
    label: str,
) -> str:
    key = str(value.get("id") or "").strip()
    if not key:
        raise ValueError(f"{label} 缺少 id")
    if key in seen:
        raise ValueError(f"{label} id 重复：{key}")
    return key


def _evidence_ids(value: dict[str, Any]) -> list[str]:
    evidence = value.get("evidence_utterance_ids")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("语义输出必须引用至少一个 utterance")
    ids = [str(item) for item in evidence]
    if len(ids) != len(set(ids)):
        raise ValueError("语义输出包含重复 utterance 引用")
    return ids


def _validate_evidence_ids(
    evidence: Sequence[str],
    utterance_by_key: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    *,
    episode_key: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    label: str,
) -> None:
    for key in evidence:
        if key not in utterance_by_key:
            raise ValueError(f"{label} 引用了不存在的 utterance：{key}")
        episode, utterance = utterance_by_key[key]
        if episode_key is not None and str(episode["key"]) != episode_key:
            raise ValueError(f"{label} 引用了其他 episode 的 utterance")
        if start_ms is not None and int(utterance["start_ms"]) < start_ms:
            raise ValueError(f"{label} 引用了 scene 起点之前的 utterance")
        if end_ms is not None and int(utterance["end_ms"]) > end_ms:
            raise ValueError(f"{label} 引用了 scene 终点之后的 utterance")


def _identity_supported(
    identity: str,
    evidence: Sequence[str],
    utterance_by_key: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    return any(
        str(utterance_by_key[key][1]["identity"]["label"]) == identity
        for key in evidence
    )


def _all_media(
    evidence: Sequence[str],
    utterance_by_key: dict[str, tuple[dict[str, Any], dict[str, Any]]],
) -> bool:
    return bool(evidence) and all(
        str(utterance_by_key[key][1]["source"]["label"]) == "media_playback"
        for key in evidence
    )


def _validate_title_body(
    value: dict[str, Any],
    *,
    label: str,
    body_key: str = "body",
) -> None:
    title = value.get("title")
    body = value.get(body_key)
    if not isinstance(title, str) or not title.strip():
        raise TypeError(f"{label} 缺少非空 title")
    if not isinstance(body, str) or not body.strip():
        raise TypeError(f"{label} 缺少非空 {body_key}")
    if len(title) > 500 or len(body) > 100_000:
        raise ValueError(f"{label} 内容过长")


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


def _review_clips(start_ms: int, end_ms: int, clip_ms: int) -> list[dict[str, Any]]:
    clips = []
    cursor = start_ms
    while cursor < end_ms:
        clip_end = min(end_ms, cursor + clip_ms)
        clips.append(
            {"index": len(clips) + 1, "start_ms": cursor, "end_ms": clip_end}
        )
        cursor = clip_end
    return clips


def _merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in ranges if end > start)
    if not ordered:
        return []
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _deduplicate_source_refs(
    refs: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
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
    ledger: dict[str, Any],
    provider_request: dict[str, Any],
    response: dict[str, Any],
    summary: dict[str, Any],
) -> Path:
    path = (
        OUTPUT_DIR
        / f"session-{session_id:06d}"
        / "semantic-v2e0"
        / f"run-{run_id:06d}.json"
    )
    payload = {
        "format": "AllDayRecording V2-E.0.2 episode manifest v1",
        "run_id": run_id,
        "config": config,
        "evidence_ledger": ledger,
        "provider_request": provider_request,
        "response": response,
        "summary": summary,
        "safety": {
            "project_runtime_network_access": False,
            "manual_evaluator_external_to_runtime": bool(config["manual_eval"]),
            "audio_bytes_included": False,
            "source_paths_included": False,
            "provider_receives_source_hashes": False,
            "provider_receives_database_token_ids": False,
            "asr_overwritten": False,
            "external_actions_written": False,
        },
    }
    _write_json(path, payload)
    return path


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}
