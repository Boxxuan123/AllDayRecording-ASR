from __future__ import annotations

import json
import secrets
import threading
import uuid
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from allday_asr.audio.tools import extract_clip
from allday_asr.config import load_config
from allday_asr.paths import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_DB_PATH,
    OUTPUT_DIR,
    recording_output_dir,
)
from allday_asr.services.manual_identity import (
    retract_manual_identity_annotation,
    save_manual_identity_annotation,
)
from allday_asr.services.daily import run_daily
from allday_asr.services.evaluation import (
    evaluate_truth,
    evaluation_truth_path,
    list_evaluation_templates,
    load_evaluation_truth,
    update_evaluation_truth_segment,
)
from allday_asr.services.quality_diarization_v2d1_review import (
    complete_possible_speech_review,
    effective_workflow_summary,
    label_possible_speech_identity,
    review_possible_speech_candidate,
)
from allday_asr.services.quality_diarization_v2d1_truth import (
    create_v2d1_review_truth,
    evaluate_v2d1_review,
    v2d1_review_evaluation_overview,
)
from allday_asr.services.quality_diarization_v2d2 import (
    run_identity_contamination_audit,
)
from allday_asr.services.quality_diarization_v2d3 import (
    review_identity_candidate,
    run_identity_candidate_mining,
)
from allday_asr.services.semantic_v2e0 import review_semantic_candidate
from allday_asr.services.semantic_v2e02 import (
    run_semantic_v2e02,
    semantic_overview,
)
from allday_asr.services.sources import (
    LogicalWindow,
    logical_window_cache_key,
    materialize_logical_window,
    resolve_session_slices,
)
from allday_asr.services.speaker_timeline import (
    MAX_AUDIO_WINDOW_MS,
    speaker_timeline_overview,
    speaker_timeline_window,
)
from allday_asr.storage.database import Database

ASSET_ROOT = Path(__file__).parent / "web_assets"
MAX_JSON_BODY = 1024 * 1024


class WebApplication:
    def __init__(
        self,
        database_path: Path,
        config_path: Path,
        *,
        token: str | None = None,
    ):
        self.database_path = database_path.resolve()
        self.config_path = config_path.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()
        self.truth_lock = threading.Lock()
        self.audio_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def database(self) -> Database:
        return Database.open(self.database_path)

    def recordings(self) -> list[dict]:
        return [
            {
                "id": int(row["id"]),
                "status": row["status"],
                "duration_ms": int(row["duration_ms"]),
                "recorded_at": row["recorded_at"],
                "timezone": row["timezone"],
                "device": row["device"],
                "source_name": Path(row["source_path"]).name,
            }
            for row in self.database().list_recordings()
        ]

    def sessions(self) -> list[dict[str, Any]]:
        database = self.database()
        values: list[dict[str, Any]] = []
        for row in reversed(database.list_recording_sessions()):
            session_id = int(row["id"])
            manifest = database.get_session_manifest(session_id)
            runs = database.list_session_processing_runs(session_id)
            workflow_runs = [
                run for run in runs if str(run["run_kind"]) == "quality_workflow_v2"
            ]
            latest_workflow = workflow_runs[-1] if workflow_runs else None
            workflow_summary = (
                _json_value(latest_workflow["summary_json"])
                if latest_workflow is not None
                else None
            ) or {}
            workflow_summary = effective_workflow_summary(
                database, workflow_summary
            )
            completed_run_kinds = {
                str(run["run_kind"])
                for run in runs
                if str(run["status"]) == "completed"
            }
            inferred_workflow_state = None
            if "semantic_v2e0" in completed_run_kinds:
                inferred_workflow_state = "semantic_ready"
            elif "quality_diarization_v2d" in completed_run_kinds:
                inferred_workflow_state = "diarization_completed"
            elif "quality_asr_v2c" in completed_run_kinds:
                inferred_workflow_state = "asr_completed"
            legacy_recording_id = (
                int(row["legacy_recording_id"])
                if row["legacy_recording_id"] is not None
                else None
            )
            if manifest is not None:
                source_name = Path(str(manifest["manifest_path"])).parent.name
                kind = "manifest"
            elif legacy_recording_id is not None:
                recording = (
                    database.get_recording(legacy_recording_id)
                )
                source_name = Path(str(recording["source_path"])).name
                kind = "legacy-file"
            else:
                source_name = str(row["session_key"])
                kind = "session"
            values.append(
                {
                    "id": session_id,
                    "recording_id": legacy_recording_id,
                    "kind": kind,
                    "status": str(row["status"]),
                    "duration_ms": int(row["duration_ms"]),
                    "recorded_at": str(row["recorded_at"]),
                    "timezone": str(row["timezone"]),
                    "device": row["device"],
                    "source_name": source_name,
                    "chunk_count": len(database.list_session_sources(session_id)),
                    "workflow_state": (
                        workflow_summary.get("workflow_state")
                        or inferred_workflow_state
                    ),
                    "workflow_status": (
                        str(latest_workflow["status"])
                        if latest_workflow is not None
                        else None
                    ),
                }
            )
        return values

    def session_dashboard(self, session_id: int) -> dict[str, Any]:
        database = self.database()
        session = database.get_recording_session(session_id)
        sources = database.list_session_sources(session_id)
        manifest = database.get_session_manifest(session_id)
        runs = database.list_session_processing_runs(session_id)
        latest = {
            kind: next(
                (
                    row
                    for row in reversed(runs)
                    if str(row["run_kind"]) == kind
                ),
                None,
            )
            for kind in (
                "quality_workflow_v2",
                "quality_asr_v2c",
                "quality_diarization_v2d",
                "semantic_v2e0",
            )
        }
        workflow_summary = _run_summary(latest["quality_workflow_v2"])
        workflow_summary = effective_workflow_summary(
            database, workflow_summary
        )
        asr_summary = _run_summary(latest["quality_asr_v2c"])
        diarization_summary = _run_summary(latest["quality_diarization_v2d"])
        semantic_summary = _run_summary(latest["semantic_v2e0"])
        integrity = workflow_summary.get("integrity") or {}
        integrity_instances = list(integrity.get("instances") or [])
        return {
            "session": {
                "id": session_id,
                "recording_id": (
                    int(session["legacy_recording_id"])
                    if session["legacy_recording_id"] is not None
                    else None
                ),
                "kind": "manifest" if manifest is not None else "legacy-file",
                "status": str(session["status"]),
                "duration_ms": int(session["duration_ms"]),
                "recorded_at": str(session["recorded_at"]),
                "timezone": str(session["timezone"]),
                "device": session["device"],
                "session_key": str(session["session_key"]),
                "chunk_count": len(sources),
                "input_fingerprint": database.session_input_fingerprint(session_id),
                "manifest_sha256": (
                    str(manifest["manifest_sha256"]) if manifest is not None else None
                ),
            },
            "workflow": _dashboard_run(latest["quality_workflow_v2"], workflow_summary),
            "asr": _dashboard_run(latest["quality_asr_v2c"], asr_summary),
            "diarization": _dashboard_run(
                latest["quality_diarization_v2d"], diarization_summary
            ),
            "semantic": _dashboard_run(latest["semantic_v2e0"], semantic_summary),
            "integrity": {
                "available": bool(integrity),
                "verified_instances": sum(
                    item.get("status") == "verified" for item in integrity_instances
                ),
                "instance_count": len(integrity_instances) or len(sources),
                "gaps": len(integrity.get("gaps") or []),
                "overlaps": len(integrity.get("overlaps") or []),
                "manifest_status": (integrity.get("manifest") or {}).get("status"),
            },
            "schema_version": database.schema_version(),
        }

    def dashboard(self, recording_id: int) -> dict:
        database = self.database()
        recording = database.get_recording(recording_id)
        segment_counts = database.segment_status_counts(recording_id)
        annotations = database.list_segment_annotations(recording_id)
        events = database.list_conversation_events(recording_id)
        candidates = database.list_action_candidates(recording_id)
        runs = database.list_processing_runs(recording_id)
        templates = list_evaluation_templates(recording_id)
        stages = {
            row["stage"]: {
                "status": row["status"],
                "model_id": row["model_id"],
                "model_version": row["model_version"],
            }
            for row in database.list_stages(recording_id)
        }
        return {
            "recording": {
                "id": recording_id,
                "status": recording["status"],
                "duration_ms": int(recording["duration_ms"]),
                "recorded_at": recording["recorded_at"],
                "timezone": recording["timezone"],
                "device": recording["device"],
            },
            "segments": segment_counts,
            "annotations": len(annotations),
            "events": len(events),
            "actions": {
                "total": len(candidates),
                "pending": sum(row["status"] == "pending" for row in candidates),
                "confirmed": sum(row["status"] == "confirmed" for row in candidates),
            },
            "evaluations": templates,
            "runs": len(runs),
            "last_run": _processing_run_payload(runs[-1]) if runs else None,
            "stages": stages,
            "schema_version": database.schema_version(),
        }

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

    def runs(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
    ) -> list[dict]:
        database = self.database()
        if session_id is not None:
            rows = database.list_session_processing_runs(session_id)
        elif recording_id is not None:
            rows = database.list_processing_runs(recording_id)
        else:
            raise ValueError("必须指定 recording_id 或 session_id")
        return [
            _processing_run_payload(row)
            for row in reversed(rows)
        ]

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
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "kind": "quality_diarization_v2d3",
            "recording_id": recording_id,
            "session_id": effective_session_id,
            "status": "queued",
            "stage": "queued",
            "detail": "等待加载本地说话人 embedding 模型",
            "result": None,
            "error": None,
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

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

        threading.Thread(
            target=worker,
            name=f"v2d3-run-{job_id[:8]}",
            daemon=True,
        ).start()
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

    def start_daily_run(self, recording_id: int) -> dict:
        self.database().get_recording(recording_id)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "recording_id": recording_id,
            "status": "queued",
            "stage": "queued",
            "detail": "等待开始",
            "result": None,
            "error": None,
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

        def worker() -> None:
            self._update_job(job_id, status="running", stage="starting", detail="读取配置")

            def progress(stage: str, detail: str) -> None:
                self._update_job(job_id, stage=stage, detail=detail)

            try:
                summary = run_daily(
                    self.database(),
                    recording_id,
                    load_config(self.config_path),
                    progress=progress,
                )
                result = {
                    "run_id": summary.run_id,
                    "recording_id": summary.recording_id,
                    "status": summary.status,
                    "steps": [asdict(step) for step in summary.steps],
                    "review_actions": summary.review_actions,
                    "manifest_markdown_path": str(
                        summary.manifest_markdown_path.resolve()
                    ),
                }
                self._update_job(
                    job_id,
                    status="completed",
                    stage="completed",
                    detail="一键离线日记已完成",
                    result=result,
                )
            except Exception as exc:
                self._update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    detail="运行失败",
                    error=str(exc),
                )

        threading.Thread(target=worker, name=f"daily-run-{job_id[:8]}", daemon=True).start()
        return dict(job)

    def job(self, job_id: str) -> dict:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"任务 {job_id} 不存在")
            return dict(job)

    def _update_job(self, job_id: str, **values: Any) -> None:
        with self.jobs_lock:
            self.jobs[job_id].update(values)

    def audio_clip(self, segment_id: int, *, mode: str = "segment") -> Path:
        if mode not in {"segment", "context"}:
            raise ValueError("音频试听模式无效")
        database = self.database()
        segment = database.get_segment(segment_id)
        recording = database.get_recording(int(segment["recording_id"]))
        if mode == "context":
            context_ms = 3_000
            start_ms = max(0, int(segment["start_ms"]) - context_ms)
            end_ms = min(
                int(recording["duration_ms"]),
                int(segment["end_ms"]) + context_ms,
            )
            directory = "web-audio-context-v1"
            filename = f"segment-{segment_id}-context.wav"
        else:
            start_ms = int(segment["start_ms"])
            end_ms = int(segment["end_ms"])
            directory = "web-audio-v3"
            filename = f"segment-{segment_id}-listening.wav"
        destination = (
            recording_output_dir(int(recording["id"]))
            / directory
            / filename
        )
        with self.audio_lock:
            if not destination.is_file():
                extract_clip(
                    Path(recording["source_path"]),
                    destination,
                    start_ms,
                    end_ms,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination

    def speaker_timeline_audio_clip(
        self,
        recording_id: int | None = None,
        *,
        session_id: int | None = None,
        start_ms: int,
        end_ms: int,
    ) -> Path:
        database = self.database()
        if session_id is None:
            if recording_id is None:
                raise ValueError("必须指定 recording_id 或 session_id")
            database.get_recording(recording_id)
            session = database.get_session_for_recording(recording_id)
            session_id = int(session["id"])
        else:
            session = database.get_recording_session(session_id)
            legacy_recording_id = session["legacy_recording_id"]
            if (
                recording_id is not None
                and (
                    legacy_recording_id is None
                    or int(legacy_recording_id) != recording_id
                )
            ):
                raise ValueError("recording_id 与 session_id 不属于同一录音会话")
        duration_ms = int(session["duration_ms"])
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
            raise ValueError("试听时间范围无效")
        if end_ms - start_ms > MAX_AUDIO_WINDOW_MS:
            raise ValueError("单次试听不能超过 120 秒")
        slices, gaps = resolve_session_slices(
            database, session_id, start_ms, end_ms
        )
        window = LogicalWindow(
            session_id=session_id,
            index=0,
            core_start_ms=start_ms,
            core_end_ms=end_ms,
            analysis_start_ms=start_ms,
            analysis_end_ms=end_ms,
            slices=slices,
            uncovered_ranges=gaps,
        )
        transform = "pcm16-16khz-mono-loudnorm-i18-v1"
        fingerprint = logical_window_cache_key(window, transform=transform)[:16]
        output_root = (
            recording_output_dir(recording_id)
            if recording_id is not None
            else OUTPUT_DIR / f"session-{session_id:06d}"
        )
        destination = (
            output_root
            / "web-speaker-timeline-audio-v1"
            / f"range-{start_ms}-{end_ms}-{fingerprint}.wav"
        )
        with self.audio_lock:
            if not destination.is_file():
                materialize_logical_window(
                    window,
                    destination,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination


class AllDayRequestHandler(BaseHTTPRequestHandler):
    server: "AllDayHTTPServer"

    def do_GET(self) -> None:
        self._handle(self._dispatch_get)

    def do_POST(self) -> None:
        self._handle(self._dispatch_post)

    def do_PUT(self) -> None:
        self._handle(self._dispatch_put)

    def _handle(self, callback) -> None:
        try:
            callback()
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"本地服务错误：{exc}"},
            )

    @property
    def application(self) -> WebApplication:
        return self.server.application

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" and self._consume_token(parsed):
            return
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "请从命令输出的安全链接打开网页"})
            return
        if parsed.path == "/":
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/app.js":
            self._send_asset("app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/styles.css":
            self._send_asset("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/api/recordings":
            self._send_json(HTTPStatus.OK, {"recordings": self.application.recordings()})
            return
        if parsed.path == "/api/sessions":
            self._send_json(HTTPStatus.OK, {"sessions": self.application.sessions()})
            return
        if parsed.path == "/api/session-dashboard":
            self._send_json(
                HTTPStatus.OK,
                self.application.session_dashboard(
                    _query_int(parsed.query, "session_id")
                ),
            )
            return
        if parsed.path == "/api/session-evaluation":
            self._send_json(
                HTTPStatus.OK,
                self.application.session_evaluation(
                    _query_int(parsed.query, "session_id")
                ),
            )
            return
        if parsed.path == "/api/dashboard":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(HTTPStatus.OK, self.application.dashboard(recording_id))
            return
        if parsed.path == "/api/evaluations":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK,
                {"evaluations": list_evaluation_templates(recording_id)},
            )
            return
        evaluation_match = _match_path(
            parsed.path, r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)"
        )
        if evaluation_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.evaluation(
                    int(evaluation_match["recording_id"]),
                    unquote(evaluation_match["name"]),
                ),
            )
            return
        if parsed.path == "/api/actions":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK, {"actions": self.application.actions(recording_id)}
            )
            return
        if parsed.path == "/api/semantic":
            recording_id, session_id = _query_target(parsed.query)
            self._send_json(
                HTTPStatus.OK,
                self.application.semantic(recording_id, session_id=session_id),
            )
            return
        if parsed.path == "/api/runs":
            recording_id, session_id = _query_target(parsed.query)
            self._send_json(
                HTTPStatus.OK,
                {
                    "runs": self.application.runs(
                        recording_id, session_id=session_id
                    )
                },
            )
            return
        if parsed.path == "/api/speaker-timeline":
            recording_id, session_id = _query_target(parsed.query)
            run_id = _query_optional_int(parsed.query, "run_id")
            self._send_json(
                HTTPStatus.OK,
                self.application.speaker_timeline(
                    recording_id, session_id=session_id, run_id=run_id
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/window":
            recording_id, session_id = _query_target(parsed.query)
            self._send_json(
                HTTPStatus.OK,
                self.application.speaker_timeline_window(
                    recording_id,
                    session_id=session_id,
                    run_id=_query_int(parsed.query, "run_id"),
                    start_ms=_query_nonnegative_int(parsed.query, "start_ms"),
                    end_ms=_query_int(parsed.query, "end_ms"),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/audio":
            recording_id, session_id = _query_target(parsed.query)
            self._send_file(
                self.application.speaker_timeline_audio_clip(
                    recording_id,
                    session_id=session_id,
                    start_ms=_query_nonnegative_int(parsed.query, "start_ms"),
                    end_ms=_query_int(parsed.query, "end_ms"),
                ),
                "audio/wav",
            )
            return
        job_match = _match_path(parsed.path, r"/api/jobs/(?P<job_id>[a-f0-9]+)")
        if job_match:
            self._send_json(HTTPStatus.OK, self.application.job(job_match["job_id"]))
            return
        audio_match = _match_path(parsed.path, r"/api/audio/(?P<segment_id>\d+)")
        if audio_match:
            mode = parse_qs(parsed.query).get("mode", ["segment"])[0]
            self._send_file(
                self.application.audio_clip(
                    int(audio_match["segment_id"]), mode=mode
                ),
                "audio/wav",
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})

    def _dispatch_post(self) -> None:
        if not self._authorized_mutation():
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/daily-run":
            recording_id = int(body["recording_id"])
            self._send_json(
                HTTPStatus.ACCEPTED, self.application.start_daily_run(recording_id)
            )
            return
        if parsed.path == "/api/speaker-timeline/possible-review":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.review_possible_speech(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                    candidate_id=str(body["candidate_id"]),
                    status=str(body["status"]),
                    note=(
                        str(body["note"])
                        if body.get("note") is not None
                        else None
                    ),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/possible-identity":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.label_possible_speech_identity(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                    candidate_id=str(body["candidate_id"]),
                    identity_label=str(body["identity_label"]),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/manual-identity":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.save_manual_identity(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                    start_ms=int(body["start_ms"]),
                    end_ms=int(body["end_ms"]),
                    identity_label=str(body["identity_label"]),
                    note=(
                        str(body["note"])
                        if body.get("note") is not None
                        else None
                    ),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/manual-identity/retract":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.retract_manual_identity(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                    annotation_id=int(body["annotation_id"]),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/possible-review/complete":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.complete_possible_speech_review(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                ),
            )
            return
        if parsed.path == "/api/session-evaluation/v2d1":
            self._send_json(
                HTTPStatus.OK,
                self.application.create_session_evaluation(
                    int(body["session_id"]), run_id=int(body["run_id"])
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/v2d2":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.run_v2d2_identity_audit(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/v2d3":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.start_v2d3_identity_mining(
                    recording_id,
                    session_id=session_id,
                    target_identity=str(body["target_identity"]),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/identity-review":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.review_identity_expansion(
                    recording_id,
                    session_id=session_id,
                    run_id=int(body["run_id"]),
                    candidate_id=str(body["candidate_id"]),
                    status=str(body["status"]),
                    note=str(body["note"]) if body.get("note") is not None else None,
                ),
            )
            return
        if parsed.path == "/api/semantic/generate":
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.generate_semantic(
                    recording_id, session_id=session_id
                ),
            )
            return
        semantic_review_match = _match_path(
            parsed.path,
            r"/api/semantic/candidates/(?P<candidate_id>\d+)/review",
        )
        if semantic_review_match:
            recording_id, session_id = _body_target(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.review_semantic(
                    recording_id,
                    int(semantic_review_match["candidate_id"]),
                    body,
                    session_id=session_id,
                ),
            )
            return
        evaluation_match = _match_path(
            parsed.path,
            r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/run",
        )
        if evaluation_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.run_evaluation(
                    int(evaluation_match["recording_id"]),
                    unquote(evaluation_match["name"]),
                ),
            )
            return
        action_match = _match_path(
            parsed.path, r"/api/actions/(?P<candidate_id>\d+)/review"
        )
        if action_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.review_action(
                    int(action_match["candidate_id"]), body
                ),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def _dispatch_put(self) -> None:
        if not self._authorized_mutation():
            return
        parsed = urlparse(self.path)
        match = _match_path(
            parsed.path,
            r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/segments/(?P<segment_id>\d+)",
        )
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        updated = self.application.update_evaluation_segment(
            int(match["recording_id"]),
            unquote(match["name"]),
            int(match["segment_id"]),
            self._read_json(),
        )
        self._send_json(HTTPStatus.OK, {"segment": updated})

    def _consume_token(self, parsed) -> bool:
        values = parse_qs(parsed.query)
        token = values.get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "安全链接已失效"})
            return True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"allday_session={self.application.token}; Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        header_token = self.headers.get("X-AllDay-Token")
        if header_token and secrets.compare_digest(header_token, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("allday_session")
        return bool(
            session
            and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求"})
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_JSON_BODY:
            raise ValueError("请求正文大小无效")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求正文不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return payload

    def _send_asset(self, filename: str, content_type: str) -> None:
        path = ASSET_ROOT / filename
        if not path.is_file():
            raise FileNotFoundError(f"网页资源不存在：{filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_file(self, path: Path, content_type: str) -> None:
        self._send_bytes(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            cache_control="private, max-age=3600",
        )

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self'; img-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")


class AllDayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: WebApplication):
        self.application = application
        super().__init__(server_address, AllDayRequestHandler)


def create_web_server(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> AllDayHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本地工作台只能绑定 127.0.0.1 或 localhost")
    application = WebApplication(database_path, config_path, token=token)
    server = AllDayHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_web(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    server = create_web_server(
        database_path=database_path,
        config_path=config_path,
        host=host,
        port=port,
    )
    url = f"{server.application.base_url}/?token={server.application.token}"
    print(f"AllDayRecording 本地工作台：{url}")
    print("仅监听本机；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _query_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    value = int(values[0])
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _query_optional_int(query: str, name: str) -> int | None:
    values = parse_qs(query).get(name)
    if not values:
        return None
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _query_target(query: str) -> tuple[int | None, int | None]:
    recording_id = _query_optional_int(query, "recording_id")
    session_id = _query_optional_int(query, "session_id")
    if recording_id is None and session_id is None:
        raise ValueError("缺少查询参数 recording_id 或 session_id")
    return recording_id, session_id


def _body_target(body: dict[str, Any]) -> tuple[int | None, int | None]:
    recording_value = body.get("recording_id")
    session_value = body.get("session_id")
    recording_id = int(recording_value) if recording_value is not None else None
    session_id = int(session_value) if session_value is not None else None
    if recording_id is None and session_id is None:
        raise ValueError("缺少 recording_id 或 session_id")
    if recording_id is not None and recording_id < 1:
        raise ValueError("recording_id 必须大于 0")
    if session_id is not None and session_id < 1:
        raise ValueError("session_id 必须大于 0")
    return recording_id, session_id


def _query_nonnegative_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"查询参数 {name} 不能为负数")
    return value


def _match_path(path: str, pattern: str) -> dict[str, str] | None:
    import re

    match = re.fullmatch(pattern, path)
    return match.groupdict() if match else None


def _json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def _processing_run_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "recording_id": (
            int(row["recording_id"]) if row["recording_id"] is not None else None
        ),
        "session_id": int(row["session_id"]),
        "run_kind": row["run_kind"],
        "status": row["status"],
        "config_sha256": row["config_sha256"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
        "summary": _json_value(row["summary_json"]),
        "artifacts": _json_value(row["artifacts_json"]),
    }


def _run_summary(row) -> dict[str, Any]:
    if row is None:
        return {}
    value = _json_value(row["summary_json"])
    return value if isinstance(value, dict) else {}


def _dashboard_run(row, summary: dict[str, Any]) -> dict[str, Any]:
    if row is None:
        return {"available": False, "summary": {}}
    return {
        "available": True,
        "id": int(row["id"]),
        "status": str(row["status"]),
        "started_at": str(row["started_at"]),
        "completed_at": row["completed_at"],
        "error": row["error"],
        "summary": summary,
    }


def _action_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "recording_id": int(row["recording_id"]),
        "type": row["candidate_type"],
        "status": row["status"],
        "title": row["title"],
        "scheduled_at": row["scheduled_at"],
        "time_text": row["time_text"],
        "location": row["location"],
        "confidence": float(row["confidence"]),
        "source_segment_ids": _json_value(row["source_segment_ids_json"]),
        "participants": _json_value(row["participants_json"]),
        "evidence": _json_value(row["evidence_json"]),
    }
