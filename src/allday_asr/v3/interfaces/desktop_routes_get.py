from __future__ import annotations

from http import HTTPStatus
from urllib.parse import parse_qs, unquote, urlparse

from allday_asr.v3.application import processing_snapshot_dict

from .desktop_http_contract import (
    _CORRECTION_ROUTE,
    _DAILY_SUMMARY_ROUTE,
    _EVENT_OPERATIONS_ROUTE,
    _JOB_ROUTE,
    _MEDIA_ROUTE,
    _PERSON_ROUTE,
    _RELATIONSHIP_OBSERVATION_ROUTE,
    _REMINDER_CANDIDATE_ROUTE,
    _REMINDER_FEEDBACK_ROUTE,
    _RUN_ROUTE,
    _SESSION_ROUTE,
    _SPEAKER_CLUSTER_ROUTE,
    _integer,
    _location_without_token,
    _reminder_datetime,
    _required_query,
)


class DesktopGetRoutesMixin:
    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if self._consume_token(parsed):
            return
        path = parsed.path.rstrip("/") or "/"
        if not self._authenticated():
            if self._recoverable_navigation(path):
                self._establish_session(_location_without_token(parsed))
                return
            self._send_error(
                HTTPStatus.FORBIDDEN,
                "desktop_session_required",
                "请打开本机 V3 工作台短地址；浏览器会自动恢复会话。",
            )
            return
        query = parse_qs(parsed.query)
        if path == "/api/v3/status":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.status())
            return
        if path == "/api/v3/overview":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.overview())
            return
        if path == "/api/v3/recording-sessions":
            cursor = query.get("cursor", [None])[0]
            limit = _integer(query.get("limit", ["50"])[0], "limit")
            page = self.application.core.desktop.list_sessions(cursor, limit)
            self._send_json(HTTPStatus.OK, page.as_dict())
            return
        if match := _SESSION_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.desktop.session_detail(unquote(match.group(1))),
            )
            return
        if path == "/api/v3/processing-jobs":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.desktop.list_processing_jobs(
                        status, limit
                    )
                },
            )
            return
        if match := _JOB_ROUTE.fullmatch(path):
            snapshot = self.application.core.processing.get(unquote(match.group(1)))
            self._send_json(HTTPStatus.OK, processing_snapshot_dict(snapshot))
            return
        if match := _RUN_ROUTE.fullmatch(path):
            snapshot = self.application.core.processing.get_for_run(
                unquote(match.group(1))
            )
            self._send_json(HTTPStatus.OK, processing_snapshot_dict(snapshot)["run"])
            return
        if match := _CORRECTION_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.corrections.correction_history(
                    unquote(match.group(1))
                ),
            )
            return
        if path == "/api/v3/reviews":
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.desktop.list_reviews(limit)},
            )
            return
        if path == "/api/v3/evidence-spans":
            session_id = _required_query(query, "session_id")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.knowledge.list_evidence(session_id)},
            )
            return
        if path == "/api/v3/events":
            session_id = _required_query(query, "session_id")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.knowledge.list_events(session_id)},
            )
            return
        if match := _EVENT_OPERATIONS_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.knowledge.event_history(
                        unquote(match.group(1))
                    )
                },
            )
            return
        if path == "/api/v3/memories":
            session_id = query.get("session_id", [None])[0]
            subject_type = query.get("subject_type", [None])[0]
            subject_id = query.get("subject_id", [None])[0]
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.knowledge.list_memories(
                        session_id=session_id,
                        subject_type=subject_type,
                        subject_id=subject_id,
                    )
                },
            )
            return
        if path == "/api/v3/knowledge-proposals":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.knowledge.list_proposals(
                        status, limit
                    )
                },
            )
            return
        if path == "/api/v3/derivations/affected":
            self._send_json(
                HTTPStatus.OK,
                self.application.core.knowledge.affected_by(
                    _required_query(query, "source_type"),
                    _required_query(query, "source_id"),
                ),
            )
            return
        if path == "/api/v3/invalidations":
            target_type = query.get("target_type", [None])[0]
            target_id = query.get("target_id", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.knowledge.list_invalidations(
                        target_type, target_id, limit
                    )
                },
            )
            return
        if path == "/api/v3/recompute-requests":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.knowledge.list_recompute_requests(
                        status, limit
                    )
                },
            )
            return
        if path == "/api/v3/reminder-candidates":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.reminders.list_candidates(
                        status, limit
                    )
                },
            )
            return
        if match := _REMINDER_FEEDBACK_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.reminders.feedback(
                        unquote(match.group(1))
                    )
                },
            )
            return
        if match := _REMINDER_CANDIDATE_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.reminders.candidate(unquote(match.group(1))),
            )
            return
        if path == "/api/v3/reminders/due":
            at = query.get("at", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.reminders.due(
                        _reminder_datetime(at) if at is not None else None,
                        limit,
                    )
                },
            )
            return
        if path == "/api/v3/reminders":
            status = query.get("status", [None])[0]
            session_id = query.get("session_id", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.reminders.list_schedules(
                        status=status, session_id=session_id, limit=limit
                    )
                },
            )
            return
        if path == "/api/v3/persons":
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.people.list_people()},
            )
            return
        if path == "/api/v3/voice-prototype-candidates":
            person_id = query.get("person_id", [None])[0]
            raw_status = query.get("status", ["pending"])[0]
            status = None if raw_status == "all" else raw_status
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.people.list_review_candidates(
                        person_id, status, limit
                    )
                },
            )
            return
        if path == "/api/v3/daily-summaries":
            limit = _integer(query.get("limit", ["31"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.insights.list_daily(limit)},
            )
            return
        if match := _DAILY_SUMMARY_ROUTE.fullmatch(path):
            timezone_name = _required_query(query, "timezone")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.insights.daily(
                    unquote(match.group(1)), timezone_name
                ),
            )
            return
        if path == "/api/v3/relationship-observations":
            person_id = query.get("person_id", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {
                    "items": self.application.core.insights.relationships(
                        person_id, limit
                    )
                },
            )
            return
        if match := _RELATIONSHIP_OBSERVATION_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.insights.relationship(unquote(match.group(1))),
            )
            return
        if match := _PERSON_ROUTE.fullmatch(path):
            limit = _integer(query.get("limit", ["200"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.person(
                    unquote(match.group(1)), limit
                ),
            )
            return
        if path == "/api/v3/speaker-clusters":
            status = query.get("status", [None])[0]
            limit = _integer(query.get("limit", ["100"])[0], "limit")
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.people.list_clusters(status, limit)},
            )
            return
        if match := _SPEAKER_CLUSTER_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.cluster(unquote(match.group(1))),
            )
            return
        if path == "/api/v3/devices":
            self._send_json(
                HTTPStatus.OK,
                {"items": self.application.core.desktop.list_devices()},
            )
            return
        if path == "/api/v3/data-health":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.data_health())
            return
        if path == "/api/v3/settings":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.settings())
            return
        if path == "/api/v3/lab":
            self._send_json(HTTPStatus.OK, self.application.core.desktop.lab())
            return
        if match := _MEDIA_ROUTE.fullmatch(path):
            self._send_media(unquote(match.group(1)))
            return
        if path == "/api/v3/events/processing":
            self._send_processing_events(query)
            return
        self._send_frontend(path)

