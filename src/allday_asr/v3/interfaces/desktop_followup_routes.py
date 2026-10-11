"""Same desktop auth and redacted /chat/ access-log boundary."""

import json

from allday_asr.v3.ports.chat_data import ChatDataError


class DesktopFollowupRoutesMixin:
    def _followup_get(self, path, query):
        if not path.startswith("/api/v3/chat/followups"):
            return False

        def run():
            if any(len(v) != 1 for v in query.values()):
                raise ChatDataError("INVALID_REQUEST", 400)
            service = self.application.followups
            if path == "/api/v3/chat/followups/status":
                return service.status()
            if path == "/api/v3/chat/followups/review-candidates" and not query:
                return service.review_candidates()
            if path == "/api/v3/chat/followups/item" and set(query) == {"event"}:
                with service.service.uow_factory().reading() as uow:
                    current = uow.knowledge.get_event(query["event"][0])
                    if not current or not current.payload.get("chat_followup"):
                        raise ChatDataError("NOT_FOUND", 404)
                    source = uow.followups.source(
                        current.payload["chat_followup"]["source_key"]
                    )
                    return {
                        "items": [
                            service.service._view(
                                {
                                    **source,
                                    "payload": current.payload,
                                    "revision": current.revision,
                                    "status": current.status.value,
                                    "conflict": json.loads(
                                        source["conflict_json"]
                                    )
                                    if source["conflict_json"]
                                    else None,
                                    "updated_at": current.updated_at.isoformat(),
                                }
                            )
                        ],
                        "has_more": False,
                    }
            if path == "/api/v3/chat/followups/items":
                try:
                    limit = int(query.get("limit", ["15"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    raise ChatDataError("INVALID_LIMIT", 400) from None
                return service.service.list(limit, offset)
            if path == "/api/v3/chat/followups/job" and set(query) == {"id"}:
                return service.public(service.service.job(query["id"][0]))
            raise ChatDataError("NOT_FOUND", 404)

        self._chat_call(run)
        return True

    def _followup_post(self, path, body):
        if not path.startswith("/api/v3/chat/followups"):
            return False

        def run():
            service = self.application.followups
            if path == "/api/v3/chat/followups/start" and set(body) <= {
                "resume_id",
                "renew_model_window",
                "additional_calls",
                "prepare_only",
                "model_budget",
            }:
                return service.start(
                    resume_id=body.get("resume_id"),
                    renew_model_window=body.get("renew_model_window", False),
                    additional_calls=body.get("additional_calls", 12),
                    prepare_only=body.get("prepare_only", False),
                    model_budget=body.get("model_budget"),
                )
            if path == "/api/v3/chat/followups/accept-candidate" and set(body) == {
                "job_id",
                "batch",
                "candidate",
                "binding",
            }:
                return service.accept_candidate(**body)
            if path == "/api/v3/chat/followups/stop" and not body:
                return service.stop()
            if path == "/api/v3/chat/followups/config" and set(body) == {
                "scopes",
                "timezone",
            }:
                return service.configure(body["scopes"], body["timezone"])
            if path == "/api/v3/chat/followups/account" and set(body) == {
                "platform",
                "source",
                "account",
                "person",
            }:
                return service.link_account(**body)
            if path == "/api/v3/chat/followups/act" and {
                "source_key",
                "action",
                "expected_revision",
            } <= set(body) <= {"source_key", "action", "expected_revision", "changes"}:
                return service.service.act(
                    body["source_key"],
                    body["action"],
                    body.get("changes"),
                    body["expected_revision"],
                )
            raise ChatDataError("INVALID_REQUEST", 400)

        self._chat_call(run)
        return True
