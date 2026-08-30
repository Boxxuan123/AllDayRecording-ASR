from __future__ import annotations

from pathlib import Path
from typing import Any

from allday_asr.interfaces.web.presenters import (
    dashboard_run as _dashboard_run,
    json_value as _json_value,
    processing_run_payload as _processing_run_payload,
    run_summary as _run_summary,
)
from allday_asr.services.evaluation import list_evaluation_templates
from allday_asr.services.quality_diarization_v2d1_review import (
    effective_workflow_summary,
)


class WorkspaceUseCases:
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
