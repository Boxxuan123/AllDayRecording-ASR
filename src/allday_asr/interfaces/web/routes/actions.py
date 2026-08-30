from __future__ import annotations

from http import HTTPStatus

from allday_asr.interfaces.web.params import match_path, query_int


def get_actions(handler, parsed) -> bool:
    if parsed.path != "/api/actions":
        return False
    recording_id = query_int(parsed.query, "recording_id")
    handler._send_json(
        HTTPStatus.OK,
        {"actions": handler.application.actions(recording_id)},
    )
    return True


def post_actions(handler, parsed, body) -> bool:
    match = match_path(
        parsed.path,
        r"/api/actions/(?P<candidate_id>\d+)/review",
    )
    if not match:
        return False
    handler._send_json(
        HTTPStatus.OK,
        handler.application.review_action(int(match["candidate_id"]), body),
    )
    return True
