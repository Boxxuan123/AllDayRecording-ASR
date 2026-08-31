from __future__ import annotations

from http import HTTPStatus

from allday_asr.interfaces.web.params import (
    match_path,
    query_int,
    query_target,
)


def get_workspace(handler, parsed) -> bool:
    if parsed.path == "/api/recordings":
        handler._send_json(
            HTTPStatus.OK,
            {"recordings": handler.application.recordings()},
        )
        return True
    if parsed.path == "/api/sessions":
        handler._send_json(
            HTTPStatus.OK,
            {"sessions": handler.application.sessions()},
        )
        return True
    if parsed.path == "/api/session-dashboard":
        handler._send_json(
            HTTPStatus.OK,
            handler.application.session_dashboard(
                query_int(parsed.query, "session_id")
            ),
        )
        return True
    if parsed.path == "/api/dashboard":
        handler._send_json(
            HTTPStatus.OK,
            handler.application.dashboard(query_int(parsed.query, "recording_id")),
        )
        return True
    if parsed.path == "/api/runs":
        recording_id, session_id = query_target(parsed.query)
        handler._send_json(
            HTTPStatus.OK,
            {
                "runs": handler.application.runs(
                    recording_id,
                    session_id=session_id,
                )
            },
        )
        return True
    job_match = match_path(parsed.path, r"/api/jobs/(?P<job_id>[a-f0-9]+)")
    if job_match:
        handler._send_json(
            HTTPStatus.OK,
            handler.application.job(job_match["job_id"]),
        )
        return True
    return False


def post_workspace(handler, parsed, body) -> bool:
    if parsed.path == "/api/daily-run":
        handler._send_json(
            HTTPStatus.ACCEPTED,
            handler.application.start_daily_run(int(body["recording_id"])),
        )
        return True
    if parsed.path == "/api/workflow-v2":
        shadow = body.get("shadow")
        if not isinstance(shadow, bool):
            raise ValueError("shadow 必须是 boolean")
        handler._send_json(
            HTTPStatus.ACCEPTED,
            handler.application.start_quality_workflow(
                int(body["session_id"]),
                shadow=shadow,
            ),
        )
        return True
    return False
