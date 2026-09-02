from __future__ import annotations

from http import HTTPStatus
from urllib.parse import unquote, urlparse

from allday_asr.v3.application import (
    CorrectUtteranceCommand,
    memory_draft_from_dict,
    observations_from_dict,
    processing_snapshot_dict,
    revision_from_dict,
)
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.people import PersonKind

from .desktop_http_contract import (
    _AUTOMATIC_WORKFLOW_RETRY_ROUTE,
    _CANCEL_ROUTE,
    _CORRECTION_ROUTE,
    _PERSON_IDENTITY_POLICY_ROUTE,
    _PERSON_MEMORIES_ROUTE,
    _PERSON_MEMORY_EXPIRE_ROUTE,
    _PERSON_MEMORY_REFRESH_ROUTE,
    _PERSON_MEMORY_RETRACT_ROUTE,
    _PERSON_MEMORY_REVISE_ROUTE,
    _PERSON_MEMORY_UNDO_ROUTE,
    _PERSON_PROFILE_ROUTE,
    _PROPOSAL_ACCEPT_ROUTE,
    _PROPOSAL_REJECT_ROUTE,
    _RELATIONSHIP_OBSERVATION_RETRACT_ROUTE,
    _RELATIONSHIP_OBSERVATION_REVISE_ROUTE,
    _RELATIONSHIP_OBSERVATION_UNDO_ROUTE,
    _REMINDER_CONFIRM_ROUTE,
    _REMINDER_DELIVER_ROUTE,
    _REMINDER_IGNORE_ROUTE,
    _REMINDER_MODIFY_ROUTE,
    _RETRY_ROUTE,
    _SESSION_RECOVERY_ROUTE,
    _SPEAKER_IGNORE_ROUTE,
    _SPEAKER_LABEL_ROUTE,
    _SPEAKER_MERGE_ROUTE,
    _SPEAKER_SPLIT_ROUTE,
    _SPEAKER_UNDO_ROUTE,
    _VOICE_PROTOTYPE_REVIEW_ROUTE,
    _generation_submission,
    _reminder_generation_submission,
)


class DesktopPostRoutesMixin:
    def _dispatch_post(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        if path == _SESSION_RECOVERY_ROUTE:
            if not self._recoverable_fetch():
                self._send_error(
                    HTTPStatus.FORBIDDEN,
                    "desktop_session_recovery_denied",
                    "只允许 V3 工作台本身恢复本机会话。",
                )
                return
            self._send_bytes(
                HTTPStatus.NO_CONTENT,
                b"",
                "application/json; charset=utf-8",
                headers={"Set-Cookie": self._session_cookie()},
            )
            return
        if not self._authorized_mutation():
            return
        body = self._read_json()
        if match := _AUTOMATIC_WORKFLOW_RETRY_ROUTE.fullmatch(path):
            if body:
                raise ValueError("automatic workflow retry request body must be empty")
            result = self.application.automatic_workflows.request_retry(
                unquote(match.group(1))
            )
            self._send_json(HTTPStatus.ACCEPTED, result)
            return
        if match := _RETRY_ROUTE.fullmatch(path):
            if body:
                raise ValueError("retry request body must be empty")
            snapshot = self.application.core.processing.retry(unquote(match.group(1)))
            self._send_json(HTTPStatus.ACCEPTED, processing_snapshot_dict(snapshot))
            return
        if match := _CANCEL_ROUTE.fullmatch(path):
            reason = body.get("reason")
            if set(body) != {"reason"} or not isinstance(reason, str) or not reason:
                raise ValueError("cancel request requires a reason")
            snapshot = self.application.core.processing.cancel(
                unquote(match.group(1)), reason
            )
            self._send_json(HTTPStatus.ACCEPTED, processing_snapshot_dict(snapshot))
            return
        if match := _CORRECTION_ROUTE.fullmatch(path):
            required = {
                "expected_revision", "text", "speaker_track_id", "identity"
            }
            if set(body) != required:
                raise ValueError("utterance correction fields are invalid")
            revision = body["expected_revision"]
            text = body["text"]
            speaker_track_id = body["speaker_track_id"]
            identity = body["identity"]
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
                or not isinstance(text, str)
                or (
                    speaker_track_id is not None
                    and not isinstance(speaker_track_id, str)
                )
                or identity not in {value.value for value in SelfIdentity}
            ):
                raise ValueError("utterance correction values are invalid")
            value = self.application.core.corrections.correct_utterance(
                CorrectUtteranceCommand(
                    utterance_id=unquote(match.group(1)),
                    expected_revision=revision,
                    text=text,
                    actor="desktop-user",
                    speaker_track_id=speaker_track_id,
                    change_speaker=True,
                    identity=SelfIdentity(identity),
                    change_identity=True,
                )
            )
            self._send_json(HTTPStatus.OK, value)
            return
        if path == "/api/v3/knowledge-generations":
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.core.knowledge.submit_generation(
                    _generation_submission(body)
                ),
            )
            return
        if path == "/api/v3/speaker-cluster-runs":
            if set(body) != {"session_id"} or not isinstance(body["session_id"], str):
                raise ValueError("speaker analysis requires session_id")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.analyze(body["session_id"]),
            )
            return
        if path == "/api/v3/speaker-clusters/rematch":
            if set(body) not in (set(), {"session_id"}):
                raise ValueError("speaker rematch fields are invalid")
            session_id = body.get("session_id")
            if session_id is not None and not isinstance(session_id, str):
                raise ValueError("speaker rematch session_id is invalid")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.rematch_existing(session_id),
            )
            return
        if path == "/api/v3/persons":
            if set(body) not in ({"display_name"}, {"display_name", "kind"}):
                raise ValueError("person fields are invalid")
            if not isinstance(body["display_name"], str) or not isinstance(
                body.get("kind", "known"), str
            ):
                raise ValueError("person values are invalid")
            self._send_json(
                HTTPStatus.CREATED,
                self.application.core.people.create_person(
                    body["display_name"], PersonKind(body.get("kind", "known"))
                ),
            )
            return
        if match := _PERSON_PROFILE_ROUTE.fullmatch(path):
            required = {"display_name", "aliases", "relationship_labels", "notes"}
            aliases = body.get("aliases")
            labels = body.get("relationship_labels")
            if (
                set(body) != required
                or not isinstance(body.get("display_name"), str)
                or not isinstance(body.get("notes"), str)
                or not isinstance(aliases, list)
                or not all(isinstance(value, str) for value in aliases)
                or not isinstance(labels, list)
                or not all(isinstance(value, str) for value in labels)
            ):
                raise ValueError("person profile fields are invalid")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.update_profile(
                    unquote(match.group(1)),
                    display_name=body["display_name"],
                    aliases=aliases,
                    relationship_labels=labels,
                    notes=body["notes"],
                ),
            )
            return
        if match := _PERSON_MEMORY_REFRESH_ROUTE.fullmatch(path):
            if body:
                raise ValueError("person memory refresh body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.refresh(unquote(match.group(1))),
            )
            return
        if match := _PERSON_MEMORIES_ROUTE.fullmatch(path):
            draft = memory_draft_from_dict(unquote(match.group(1)), body)
            self._send_json(
                HTTPStatus.CREATED,
                self.application.core.person_memory.create(draft),
            )
            return
        if match := _PERSON_MEMORY_REVISE_ROUTE.fullmatch(path):
            values = revision_from_dict(body)
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.revise(
                    unquote(match.group(1)), **values
                ),
            )
            return
        if match := _PERSON_MEMORY_EXPIRE_ROUTE.fullmatch(path):
            if body:
                raise ValueError("person memory expire body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.expire(unquote(match.group(1))),
            )
            return
        if match := _PERSON_MEMORY_RETRACT_ROUTE.fullmatch(path):
            if body:
                raise ValueError("person memory retract body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.retract(unquote(match.group(1))),
            )
            return
        if match := _PERSON_MEMORY_UNDO_ROUTE.fullmatch(path):
            if body:
                raise ValueError("person memory undo body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.person_memory.undo(unquote(match.group(1))),
            )
            return
        if path == "/api/v3/daily-summaries/generate":
            required = {"summary_date", "timezone"}
            allowed = required | {"reasoning_effort"}
            if set(body) - allowed or not required.issubset(body):
                raise ValueError("daily summary generation fields are invalid")
            summary_date = body["summary_date"]
            timezone_name = body["timezone"]
            effort = body.get("reasoning_effort")
            if (
                not isinstance(summary_date, str)
                or not isinstance(timezone_name, str)
                or (effort is not None and not isinstance(effort, str))
            ):
                raise ValueError("daily summary generation values are invalid")
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.core.insights.generate_daily(
                    summary_date,
                    timezone_name,
                    reasoning_effort=effort,
                ),
            )
            return
        if path == "/api/v3/relationship-observations/generate":
            required = {"person_id", "window_days", "end_date", "timezone"}
            allowed = required | {"reasoning_effort"}
            if set(body) - allowed or not required.issubset(body):
                raise ValueError("relationship generation fields are invalid")
            effort = body.get("reasoning_effort")
            if (
                not isinstance(body["person_id"], str)
                or isinstance(body["window_days"], bool)
                or not isinstance(body["window_days"], int)
                or not isinstance(body["end_date"], str)
                or not isinstance(body["timezone"], str)
                or (effort is not None and not isinstance(effort, str))
            ):
                raise ValueError("relationship generation values are invalid")
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.core.insights.generate_relationship(
                    body["person_id"],
                    body["window_days"],
                    body["end_date"],
                    body["timezone"],
                    reasoning_effort=effort,
                ),
            )
            return
        if match := _RELATIONSHIP_OBSERVATION_REVISE_ROUTE.fullmatch(path):
            if set(body) != {"observations"}:
                raise ValueError("relationship revision fields are invalid")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.insights.revise_relationship(
                    unquote(match.group(1)),
                    observations_from_dict(body["observations"]),
                ),
            )
            return
        if match := _RELATIONSHIP_OBSERVATION_RETRACT_ROUTE.fullmatch(path):
            if body:
                raise ValueError("relationship retract body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.insights.retract_relationship(
                    unquote(match.group(1))
                ),
            )
            return
        if match := _RELATIONSHIP_OBSERVATION_UNDO_ROUTE.fullmatch(path):
            if body:
                raise ValueError("relationship undo body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.insights.undo_relationship(
                    unquote(match.group(1))
                ),
            )
            return
        if match := _SPEAKER_LABEL_ROUTE.fullmatch(path):
            cluster_id = unquote(match.group(1))
            if set(body) == {"person_id"} and isinstance(body["person_id"], str):
                result = self.application.core.people.label_cluster(
                    cluster_id, body["person_id"]
                )
            elif set(body) == {"display_name"} and isinstance(
                body["display_name"], str
            ):
                result = self.application.core.people.create_and_label(
                    cluster_id, body["display_name"]
                )
            else:
                raise ValueError("speaker label requires person_id or display_name")
            self._send_json(HTTPStatus.OK, result)
            return
        if match := _SPEAKER_MERGE_ROUTE.fullmatch(path):
            sources = body.get("source_cluster_ids")
            if set(body) != {"source_cluster_ids"} or not isinstance(sources, list) or not all(
                isinstance(value, str) for value in sources
            ):
                raise ValueError("speaker merge requires source_cluster_ids")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.merge(
                    tuple(sources), unquote(match.group(1))
                ),
            )
            return
        if match := _SPEAKER_SPLIT_ROUTE.fullmatch(path):
            track_ids = body.get("speaker_track_ids")
            if set(body) != {"speaker_track_ids"} or not isinstance(track_ids, list) or not all(
                isinstance(value, str) for value in track_ids
            ):
                raise ValueError("speaker split requires speaker_track_ids")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.split(
                    unquote(match.group(1)), tuple(track_ids)
                ),
            )
            return
        if match := _SPEAKER_IGNORE_ROUTE.fullmatch(path):
            reason = body.get("reason")
            if set(body) != {"reason"} or not isinstance(reason, str):
                raise ValueError("speaker ignore requires a reason")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.ignore(
                    unquote(match.group(1)), reason
                ),
            )
            return
        if match := _SPEAKER_UNDO_ROUTE.fullmatch(path):
            if body:
                raise ValueError("speaker undo request body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.undo(unquote(match.group(1))),
            )
            return
        if match := _VOICE_PROTOTYPE_REVIEW_ROUTE.fullmatch(path):
            allowed = {"person_id", "decision", "note"}
            if (
                set(body) - allowed
                or not {"person_id", "decision"}.issubset(body)
                or not isinstance(body.get("person_id"), str)
                or not isinstance(body.get("decision"), str)
                or not isinstance(body.get("note", ""), str)
            ):
                raise ValueError("voice prototype review fields are invalid")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.review_prototype(
                    unquote(match.group(1)),
                    body["person_id"],
                    body["decision"],
                    note=body.get("note", ""),
                ),
            )
            return
        if match := _PERSON_IDENTITY_POLICY_ROUTE.fullmatch(path):
            enabled = body.get("auto_match_enabled")
            if set(body) != {"auto_match_enabled"} or not isinstance(enabled, bool):
                raise ValueError("person identity policy fields are invalid")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.people.update_identity_policy(
                    unquote(match.group(1)), auto_match_enabled=enabled
                ),
            )
            return
        if path == "/api/v3/reminder-generations":
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.core.reminders.submit_generation(
                    _reminder_generation_submission(body)
                ),
            )
            return
        if path == "/api/v3/reminder-generations/codex":
            if set(body) not in ({"session_id"}, {"session_id", "reasoning_effort"}):
                raise ValueError("Codex reminder generation fields are invalid")
            session_id = body.get("session_id")
            effort = body.get("reasoning_effort")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("Codex reminder generation session_id is required")
            if effort is not None and not isinstance(effort, str):
                raise ValueError("Codex reminder reasoning_effort is invalid")
            self._send_json(
                HTTPStatus.ACCEPTED,
                self.application.core.reminder_extraction.extract(
                    session_id,
                    reasoning_effort=effort,
                ),
            )
            return
        if match := _PROPOSAL_ACCEPT_ROUTE.fullmatch(path):
            if body:
                raise ValueError("proposal accept request body must be empty")
            resolution = self.application.core.knowledge.accept_proposal(
                unquote(match.group(1)), "desktop-user"
            )
            self._send_json(HTTPStatus.OK, resolution.as_dict())
            return
        if match := _PROPOSAL_REJECT_ROUTE.fullmatch(path):
            reason = body.get("reason")
            if set(body) != {"reason"} or not isinstance(reason, str) or not reason:
                raise ValueError("proposal rejection requires a reason")
            resolution = self.application.core.knowledge.reject_proposal(
                unquote(match.group(1)), "desktop-user", reason
            )
            self._send_json(HTTPStatus.OK, resolution.as_dict())
            return
        if match := _REMINDER_CONFIRM_ROUTE.fullmatch(path):
            if body:
                raise ValueError("reminder confirmation body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.reminders.confirm(
                    unquote(match.group(1)), "desktop-user"
                ),
            )
            return
        if match := _REMINDER_MODIFY_ROUTE.fullmatch(path):
            self._send_json(
                HTTPStatus.OK,
                self.application.core.reminders.modify(
                    unquote(match.group(1)), "desktop-user", body
                ),
            )
            return
        if match := _REMINDER_IGNORE_ROUTE.fullmatch(path):
            reason = body.get("reason")
            if set(body) != {"reason"} or not isinstance(reason, str) or not reason:
                raise ValueError("reminder ignore request requires a reason")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.reminders.ignore(
                    unquote(match.group(1)), "desktop-user", reason
                ),
            )
            return
        if match := _REMINDER_DELIVER_ROUTE.fullmatch(path):
            if body:
                raise ValueError("reminder delivery body must be empty")
            self._send_json(
                HTTPStatus.OK,
                self.application.core.reminders.deliver(unquote(match.group(1))),
            )
            return
        self._send_error(HTTPStatus.NOT_FOUND, "not_found", "V3 命令不存在。")

