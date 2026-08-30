from __future__ import annotations

from typing import Any

from allday_asr.config import load_config
from allday_asr.services.manual_identity import (
    retract_manual_identity_annotation,
    save_manual_identity_annotation,
)
from allday_asr.services.quality_diarization_v2d1_review import (
    complete_possible_speech_review,
    label_possible_speech_identity,
    review_possible_speech_candidate,
)
from allday_asr.services.quality_diarization_v2d1_truth import (
    create_v2d1_review_truth,
)
from allday_asr.services.quality_diarization_v2d2 import (
    run_identity_contamination_audit,
)
from allday_asr.services.quality_diarization_v2d3 import (
    review_identity_candidate,
    run_identity_candidate_mining,
)
from allday_asr.services.speaker_timeline import (
    speaker_timeline_overview,
    speaker_timeline_window,
)
from allday_asr.storage.database import Database


class TimelineUseCases:
    def speaker_timeline(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int | None = None,
    ) -> dict:
        return speaker_timeline_overview(
            self.database(), recording_id, session_id=session_id, run_id=run_id
        )

    def speaker_timeline_window(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        start_ms: int,
        end_ms: int,
    ) -> dict:
        return speaker_timeline_window(
            self.database(),
            recording_id,
            session_id=session_id,
            run_id=run_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def review_identity_expansion(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        candidate_id: str,
        status: str,
        note: str | None = None,
    ) -> dict:
        database = self.database()
        run = database.get_processing_run(run_id)
        if session_id is not None:
            if int(run["session_id"]) != session_id:
                raise ValueError("V2-D.3 run 不属于当前录音会话")
        elif recording_id is None or int(run["recording_id"]) != recording_id:
            raise ValueError("V2-D.3 run 不属于当前录音")
        return review_identity_candidate(
            database,
            run_id,
            candidate_id=candidate_id,
            status=status,
            note=note,
        )

    def save_manual_identity(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        start_ms: int,
        end_ms: int,
        identity_label: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        database = self.database()
        run = database.get_processing_run(run_id)
        effective_session_id = int(run["session_id"] or 0)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("人物真值采样只适用于 V2-D run")
        if session_id is not None:
            if effective_session_id != session_id:
                raise ValueError("V2-D run 不属于当前录音会话")
        elif recording_id is None or int(run["recording_id"] or 0) != recording_id:
            raise ValueError("V2-D run 不属于当前录音")
        return save_manual_identity_annotation(
            database,
            session_id=effective_session_id,
            diarization_run_id=run_id,
            start_ms=start_ms,
            end_ms=end_ms,
            identity_label=identity_label,
            note=note,
        )

    def retract_manual_identity(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        annotation_id: int,
    ) -> dict[str, Any]:
        database = self.database()
        run = database.get_processing_run(run_id)
        effective_session_id = int(run["session_id"] or 0)
        if str(run["run_kind"]) != "quality_diarization_v2d":
            raise ValueError("人物真值采样只适用于 V2-D run")
        if session_id is not None:
            if effective_session_id != session_id:
                raise ValueError("V2-D run 不属于当前录音会话")
        elif recording_id is None or int(run["recording_id"] or 0) != recording_id:
            raise ValueError("V2-D run 不属于当前录音")
        return retract_manual_identity_annotation(
            database,
            annotation_id,
            session_id=effective_session_id,
            diarization_run_id=run_id,
        )

    def review_possible_speech(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        candidate_id: str,
        status: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        database = self.database()
        self._validate_session_run_target(
            database,
            run_id,
            recording_id=recording_id,
            session_id=session_id,
        )
        return review_possible_speech_candidate(
            database,
            run_id,
            candidate_id=candidate_id,
            status=status,
            note=note,
        )

    def label_possible_speech_identity(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
        candidate_id: str,
        identity_label: str,
    ) -> dict[str, Any]:
        database = self.database()
        self._validate_session_run_target(
            database,
            run_id,
            recording_id=recording_id,
            session_id=session_id,
        )
        return label_possible_speech_identity(
            database,
            run_id,
            candidate_id=candidate_id,
            identity_label=identity_label,
        )

    def complete_possible_speech_review(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
    ) -> dict[str, Any]:
        database = self.database()
        self._validate_session_run_target(
            database,
            run_id,
            recording_id=recording_id,
            session_id=session_id,
        )
        return complete_possible_speech_review(database, run_id)

    def run_v2d2_identity_audit(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        run_id: int,
    ) -> dict[str, Any]:
        database = self.database()
        self._validate_session_run_target(
            database,
            run_id,
            recording_id=recording_id,
            session_id=session_id,
        )
        v2d1_run = database.get_processing_run(run_id)
        diarization_run_id = int(v2d1_run["parent_run_id"] or 0)
        if not diarization_run_id:
            raise ValueError("V2-D.1 run 缺少 V2-D 父运行")
        effective_session_id = int(v2d1_run["session_id"])
        truth = create_v2d1_review_truth(
            database, run_id, include_identities=True
        )
        summary = run_identity_contamination_audit(
            database,
            recording_id,
            session_id=effective_session_id,
            diarization_run_id=diarization_run_id,
            truth_set_id=int(truth["truth_set_id"]),
        )
        return {
            "run_id": summary.run_id,
            "truth": truth,
            "timeline": speaker_timeline_overview(
                database,
                recording_id,
                session_id=effective_session_id,
                run_id=diarization_run_id,
            ),
        }

    def start_v2d3_identity_mining(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        target_identity: str,
    ) -> dict[str, Any]:
        database = self.database()
        timeline = speaker_timeline_overview(
            database, recording_id, session_id=session_id
        )
        if not timeline.get("available"):
            raise ValueError("当前会话没有可用的 V2-D 结果")
        audit = timeline.get("v2d2") or {}
        if not audit.get("available"):
            raise ValueError("请先完成 V2-D.2 人工身份污染审计")
        allowed = set((timeline.get("v2d3") or {}).get("target_identities") or [])
        if target_identity not in allowed:
            raise ValueError("目标人物不属于当前冻结身份真值")
        if len(allowed) < 2:
            raise ValueError("至少需要两个明确人物，才能建立身份负对照")
        effective_session_id = int(timeline["session_id"])
        diarization_run_id = int(timeline["run"]["id"])
        truth_set_id = int(audit["truth_set_id"])
        job = self.job_registry.create(
            kind="quality_diarization_v2d3",
            recording_id=recording_id,
            session_id=effective_session_id,
            detail="等待加载本地说话人 embedding 模型",
        )
        job_id = str(job["id"])

        def worker() -> None:
            self._update_job(
                job_id,
                status="running",
                stage="embedding",
                detail=f"正在为{target_identity}生成对照式身份候选",
            )
            try:
                summary = run_identity_candidate_mining(
                    self.database(),
                    recording_id,
                    session_id=effective_session_id,
                    diarization_run_id=diarization_run_id,
                    truth_set_id=truth_set_id,
                    target_identity=target_identity,
                    device=load_config(self.config_path).runtime.device,
                )
                self._update_job(
                    job_id,
                    status="completed",
                    stage="completed",
                    detail="V2-D.3 身份扩样候选已生成",
                    result={
                        "run_id": summary.run_id,
                        "target_identity": summary.target_identity,
                        "selected_candidates": summary.selected_candidates,
                        "scored_candidate_windows": summary.scored_candidate_windows,
                    },
                )
            except Exception as exc:
                self._update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    detail="V2-D.3 运行失败",
                    error=str(exc),
                )

        self.job_registry.launch(
            target=worker,
            name=f"v2d3-run-{job_id[:8]}",
        )
        return dict(job)

    @staticmethod
    def _validate_session_run_target(
        database: Database,
        run_id: int,
        *,
        recording_id: int | None,
        session_id: int | None,
    ) -> None:
        run = database.get_processing_run(run_id)
        if session_id is not None:
            if int(run["session_id"]) != session_id:
                raise ValueError("V2-D.1 run 不属于当前录音会话")
        elif recording_id is None or int(run["recording_id"]) != recording_id:
            raise ValueError("V2-D.1 run 不属于当前录音")
