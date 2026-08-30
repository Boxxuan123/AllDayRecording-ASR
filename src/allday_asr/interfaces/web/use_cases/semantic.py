from __future__ import annotations

from typing import Any

from allday_asr.application.semantic.overview import overview as semantic_overview
from allday_asr.application.semantic.pipeline import run as run_semantic_v2e02
from allday_asr.application.semantic.review import (
    review_candidate as review_semantic_candidate,
)


class SemanticUseCases:
    def semantic(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        return semantic_overview(
            self.database(), recording_id, session_id=session_id
        )

    def generate_semantic(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        if session_id is None:
            if recording_id is None:
                raise ValueError("必须指定 recording_id 或 session_id")
            session_id = int(
                self.database().get_session_for_recording(recording_id)["id"]
            )
        summary = run_semantic_v2e02(
            self.database(), recording_id, session_id=session_id
        )
        return {
            "run_id": summary.run_id,
            "recording_id": recording_id,
            "session_id": session_id,
            "asr_run_id": summary.asr_run_id,
            "diarization_run_id": summary.diarization_run_id,
            "episode_count": summary.episode_count,
            "excluded_block_count": summary.excluded_block_count,
            "llm_job_count": summary.llm_job_count,
            "llm_payload_bytes": summary.llm_payload_bytes,
            "token_count": summary.token_count,
            "candidate_count": summary.candidate_count,
            "scene_count": summary.scene_count,
            "claim_count": summary.claim_count,
            "action_count": summary.action_count,
            "unresolved_count": summary.unresolved_count,
            "request_sha256": summary.request_sha256,
            "provider_request_sha256": summary.provider_request_sha256,
            "response_sha256": summary.response_sha256,
        }

    def review_semantic(
        self,
        recording_id: int | None,
        candidate_id: int,
        values: dict[str, Any],
        *,
        session_id: int | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "recording_id",
            "session_id",
            "status",
            "title",
            "body",
            "note",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{', '.join(sorted(unknown))}")
        if "status" not in values:
            raise ValueError("缺少 status")
        return review_semantic_candidate(
            self.database(),
            recording_id,
            candidate_id,
            session_id=session_id,
            status=str(values["status"]),
            title=str(values["title"]) if values.get("title") is not None else None,
            body=str(values["body"]) if values.get("body") is not None else None,
            note=str(values["note"]) if values.get("note") is not None else None,
        )
