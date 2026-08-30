from __future__ import annotations

from typing import Any

from allday_asr.interfaces.web.presenters import action_payload as _action_payload
from allday_asr.services.evaluation import (
    evaluate_truth,
    evaluation_truth_path,
    load_evaluation_truth,
    update_evaluation_truth_segment,
)
from allday_asr.services.quality_diarization_v2d1_truth import (
    evaluate_v2d1_review,
    v2d1_review_evaluation_overview,
)


class EvaluationUseCases:
    def evaluation(self, recording_id: int, name: str) -> dict:
        metadata, rows = load_evaluation_truth(recording_id, name)
        return {
            "metadata": metadata,
            "segments": [
                {
                    **row,
                    "audio_url": f"/api/audio/{int(row['segment_id'])}?v=3",
                    "context_audio_url": (
                        f"/api/audio/{int(row['segment_id'])}?mode=context&v=1"
                    ),
                }
                for row in rows
            ],
        }

    def update_evaluation_segment(
        self, recording_id: int, name: str, segment_id: int, values: dict
    ) -> dict:
        with self.truth_lock:
            return update_evaluation_truth_segment(
                recording_id, name, segment_id, values
            )

    def run_evaluation(self, recording_id: int, name: str) -> dict:
        summary = evaluate_truth(
            self.database(), evaluation_truth_path(recording_id, name)
        )
        return {
            "evaluation_run_id": summary.evaluation_run_id,
            "recording_id": summary.recording_id,
            "item_count": summary.item_count,
            "metrics": summary.metrics,
            "report_markdown_path": str(summary.report_markdown_path.resolve()),
            "report_json_path": str(summary.report_json_path.resolve()),
        }

    def session_evaluation(self, session_id: int) -> dict[str, Any]:
        self.database().get_recording_session(session_id)
        return v2d1_review_evaluation_overview(self.database(), session_id)

    def create_session_evaluation(
        self, session_id: int, *, run_id: int
    ) -> dict[str, Any]:
        database = self.database()
        self._validate_session_run_target(
            database, run_id, recording_id=None, session_id=session_id
        )
        return evaluate_v2d1_review(database, run_id)

    def actions(self, recording_id: int) -> list[dict]:
        return [_action_payload(row) for row in self.database().list_action_candidates(recording_id)]

    def review_action(self, candidate_id: int, values: dict) -> dict:
        allowed = {"status", "title", "scheduled_at", "location"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{', '.join(sorted(unknown))}")
        if "status" not in values:
            raise ValueError("缺少 status")
        row = self.database().review_action_candidate(
            candidate_id,
            status=str(values["status"]),
            title=values.get("title"),
            scheduled_at=values.get("scheduled_at"),
            location=values.get("location"),
        )
        return _action_payload(row)
