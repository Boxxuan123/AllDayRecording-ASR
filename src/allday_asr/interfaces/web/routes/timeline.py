from __future__ import annotations

from http import HTTPStatus

from allday_asr.interfaces.web.params import (
    body_target,
    query_int,
    query_nonnegative_int,
    query_optional_int,
    query_target,
)


def get_timeline(handler, parsed) -> bool:
    if parsed.path == "/api/speaker-timeline":
        recording_id, session_id = query_target(parsed.query)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.speaker_timeline(
                recording_id,
                session_id=session_id,
                run_id=query_optional_int(parsed.query, "run_id"),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/window":
        recording_id, session_id = query_target(parsed.query)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.speaker_timeline_window(
                recording_id,
                session_id=session_id,
                run_id=query_int(parsed.query, "run_id"),
                start_ms=query_nonnegative_int(parsed.query, "start_ms"),
                end_ms=query_int(parsed.query, "end_ms"),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/audio":
        recording_id, session_id = query_target(parsed.query)
        handler._send_file(
            handler.application.speaker_timeline_audio_clip(
                recording_id,
                session_id=session_id,
                start_ms=query_nonnegative_int(parsed.query, "start_ms"),
                end_ms=query_int(parsed.query, "end_ms"),
            ),
            "audio/wav",
        )
        return True
    return False


def post_timeline(handler, parsed, body) -> bool:
    if parsed.path == "/api/speaker-timeline/possible-review":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.review_possible_speech(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
                candidate_id=str(body["candidate_id"]),
                status=str(body["status"]),
                note=str(body["note"]) if body.get("note") is not None else None,
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/possible-identity":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.label_possible_speech_identity(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
                candidate_id=str(body["candidate_id"]),
                identity_label=str(body["identity_label"]),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/manual-identity":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.save_manual_identity(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
                start_ms=int(body["start_ms"]),
                end_ms=int(body["end_ms"]),
                identity_label=str(body["identity_label"]),
                note=str(body["note"]) if body.get("note") is not None else None,
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/manual-identity/retract":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.retract_manual_identity(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
                annotation_id=int(body["annotation_id"]),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/possible-review/complete":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.complete_possible_speech_review(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/v2d2":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.run_v2d2_identity_audit(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/v2d3":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.ACCEPTED,
            handler.application.start_v2d3_identity_mining(
                recording_id,
                session_id=session_id,
                target_identity=str(body["target_identity"]),
            ),
        )
        return True
    if parsed.path == "/api/speaker-timeline/identity-review":
        recording_id, session_id = body_target(body)
        handler._send_json(
            HTTPStatus.OK,
            handler.application.review_identity_expansion(
                recording_id,
                session_id=session_id,
                run_id=int(body["run_id"]),
                candidate_id=str(body["candidate_id"]),
                status=str(body["status"]),
                note=str(body["note"]) if body.get("note") is not None else None,
            ),
        )
        return True
    return False
