from __future__ import annotations

from http import HTTPStatus
from urllib.parse import unquote

from allday_asr.interfaces.web.params import match_path, query_int
from allday_asr.services.evaluation import list_evaluation_templates


def get_evaluation(handler, parsed) -> bool:
    if parsed.path == "/api/session-evaluation":
        handler._send_json(
            HTTPStatus.OK,
            handler.application.session_evaluation(
                query_int(parsed.query, "session_id")
            ),
        )
        return True
    if parsed.path == "/api/evaluations":
        recording_id = query_int(parsed.query, "recording_id")
        handler._send_json(
            HTTPStatus.OK,
            {"evaluations": list_evaluation_templates(recording_id)},
        )
        return True
    match = match_path(
        parsed.path,
        r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)",
    )
    if not match:
        return False
    handler._send_json(
        HTTPStatus.OK,
        handler.application.evaluation(
            int(match["recording_id"]),
            unquote(match["name"]),
        ),
    )
    return True


def post_evaluation(handler, parsed, body) -> bool:
    if parsed.path == "/api/session-evaluation/v2d1":
        handler._send_json(
            HTTPStatus.OK,
            handler.application.create_session_evaluation(
                int(body["session_id"]),
                run_id=int(body["run_id"]),
            ),
        )
        return True
    match = match_path(
        parsed.path,
        r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/run",
    )
    if not match:
        return False
    handler._send_json(
        HTTPStatus.OK,
        handler.application.run_evaluation(
            int(match["recording_id"]),
            unquote(match["name"]),
        ),
    )
    return True


def put_evaluation(handler, parsed, body) -> bool:
    match = match_path(
        parsed.path,
        r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/segments/"
        r"(?P<segment_id>\d+)",
    )
    if not match:
        return False
    updated = handler.application.update_evaluation_segment(
        int(match["recording_id"]),
        unquote(match["name"]),
        int(match["segment_id"]),
        body,
    )
    handler._send_json(HTTPStatus.OK, {"segment": updated})
    return True
