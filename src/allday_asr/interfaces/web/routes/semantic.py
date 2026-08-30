from __future__ import annotations

from http import HTTPStatus

from allday_asr.interfaces.web.params import body_target, match_path, query_target


def get_semantic(handler, parsed) -> bool:
    if parsed.path != "/api/semantic":
        return False
    recording_id, session_id = query_target(parsed.query)
    handler._send_json(
        HTTPStatus.OK,
        handler.application.semantic(recording_id, session_id=session_id),
    )
    return True


def post_semantic(handler, parsed, body) -> bool:
    if parsed.path == "/api/semantic/generate":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.generate_semantic(
                recording_id,
                session_id=session_id,
            ),
        )
        return True
    match = match_path(
        parsed.path,
        r"/api/semantic/candidates/(?P<candidate_id>\d+)/review",
    )
    if not match:
        return False
    recording_id, session_id = body_target(body)
    handler._send_json(
        HTTPStatus.OK,
        handler.application.review_semantic(
            recording_id,
            int(match["candidate_id"]),
            body,
            session_id=session_id,
        ),
    )
    return True
