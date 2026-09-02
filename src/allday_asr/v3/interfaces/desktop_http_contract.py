from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from allday_asr.v3.domain.knowledge import (
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.domain.reminders import (
    CommitmentDirection,
    ReminderGenerationSubmission,
    ReminderIntent,
    ReminderOperation,
)

ASSET_ROOT = Path(__file__).parents[1] / "web_assets"
_JOB_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)$")
_RUN_ROUTE = re.compile(r"^/api/v3/processing-runs/([^/]+)$")
_SESSION_ROUTE = re.compile(r"^/api/v3/recording-sessions/([^/]+)$")
_MEDIA_ROUTE = re.compile(r"^/api/v3/media/([^/]+)$")
_RETRY_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)/retry$")
_CANCEL_ROUTE = re.compile(r"^/api/v3/processing-jobs/([^/]+)/cancel$")
_CORRECTION_ROUTE = re.compile(r"^/api/v3/utterances/([^/]+)/corrections$")
_EVENT_OPERATIONS_ROUTE = re.compile(r"^/api/v3/events/([^/]+)/operations$")
_PROPOSAL_ACCEPT_ROUTE = re.compile(
    r"^/api/v3/knowledge-proposals/([^/]+)/accept$"
)
_PROPOSAL_REJECT_ROUTE = re.compile(
    r"^/api/v3/knowledge-proposals/([^/]+)/reject$"
)
_REMINDER_CANDIDATE_ROUTE = re.compile(r"^/api/v3/reminder-candidates/([^/]+)$")
_REMINDER_FEEDBACK_ROUTE = re.compile(
    r"^/api/v3/reminder-candidates/([^/]+)/feedback$"
)
_REMINDER_CONFIRM_ROUTE = re.compile(
    r"^/api/v3/reminder-candidates/([^/]+)/confirm$"
)
_REMINDER_MODIFY_ROUTE = re.compile(
    r"^/api/v3/reminder-candidates/([^/]+)/modify$"
)
_REMINDER_IGNORE_ROUTE = re.compile(
    r"^/api/v3/reminder-candidates/([^/]+)/ignore$"
)
_REMINDER_DELIVER_ROUTE = re.compile(r"^/api/v3/reminders/([^/]+)/deliver$")
_SPEAKER_CLUSTER_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)$")
_SPEAKER_LABEL_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)/label$")
_SPEAKER_MERGE_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)/merge$")
_SPEAKER_SPLIT_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)/split$")
_SPEAKER_IGNORE_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)/ignore$")
_SPEAKER_UNDO_ROUTE = re.compile(r"^/api/v3/speaker-clusters/([^/]+)/undo$")
_VOICE_PROTOTYPE_REVIEW_ROUTE = re.compile(
    r"^/api/v3/voice-prototypes/([^/]+)/reviews$"
)
_PERSON_ROUTE = re.compile(r"^/api/v3/persons/([^/]+)$")
_PERSON_PROFILE_ROUTE = re.compile(r"^/api/v3/persons/([^/]+)/profile$")
_PERSON_IDENTITY_POLICY_ROUTE = re.compile(
    r"^/api/v3/persons/([^/]+)/identity-policy$"
)
_PERSON_MEMORY_REFRESH_ROUTE = re.compile(
    r"^/api/v3/persons/([^/]+)/memories/refresh$"
)
_PERSON_MEMORIES_ROUTE = re.compile(r"^/api/v3/persons/([^/]+)/memories$")
_PERSON_MEMORY_REVISE_ROUTE = re.compile(
    r"^/api/v3/person-memories/([^/]+)/revise$"
)
_PERSON_MEMORY_EXPIRE_ROUTE = re.compile(
    r"^/api/v3/person-memories/([^/]+)/expire$"
)
_PERSON_MEMORY_RETRACT_ROUTE = re.compile(
    r"^/api/v3/person-memories/([^/]+)/retract$"
)
_PERSON_MEMORY_UNDO_ROUTE = re.compile(
    r"^/api/v3/person-memories/([^/]+)/undo$"
)
_DAILY_SUMMARY_ROUTE = re.compile(r"^/api/v3/daily-summaries/([^/]+)$")
_RELATIONSHIP_OBSERVATION_ROUTE = re.compile(
    r"^/api/v3/relationship-observations/([^/]+)$"
)
_RELATIONSHIP_OBSERVATION_REVISE_ROUTE = re.compile(
    r"^/api/v3/relationship-observations/([^/]+)/revise$"
)
_RELATIONSHIP_OBSERVATION_RETRACT_ROUTE = re.compile(
    r"^/api/v3/relationship-observations/([^/]+)/retract$"
)
_RELATIONSHIP_OBSERVATION_UNDO_ROUTE = re.compile(
    r"^/api/v3/relationship-observations/([^/]+)/undo$"
)
_FRONTEND_ROUTES = {
    "/",
    "/recordings",
    "/reviews",
    "/reminders",
    "/people",
    "/insights",
    "/processing",
    "/devices",
    "/data",
    "/settings",
    "/lab",
}
_SESSION_RECOVERY_ROUTE = "/api/v3/desktop-session"

def _integer(value: str, label: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be an integer") from exc
    return parsed


def _is_frontend_path(path: str) -> bool:
    return path in _FRONTEND_ROUTES or path.startswith("/recordings/")


def _location_without_token(parsed) -> str:
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query) if key != "token"]
    )
    return f"{parsed.path or '/'}{'?' + query if query else ''}"


def _required_query(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [None])[0]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} query parameter is required")
    return value


def _generation_submission(body: dict[str, Any]) -> GenerationSubmission:
    required = {
        "layer",
        "producer",
        "producer_version",
        "model",
        "prompt_version",
        "extractor_version",
        "input_scope",
        "proposals",
    }
    if set(body) != required:
        raise ValueError("generation submission fields are invalid")
    for name in (
        "producer",
        "producer_version",
        "model",
        "prompt_version",
        "extractor_version",
    ):
        if not isinstance(body[name], str) or not body[name].strip():
            raise ValueError(f"generation {name} is required")
    if not isinstance(body["input_scope"], dict):
        raise ValueError("generation input_scope must be an object")
    raw_proposals = body["proposals"]
    if not isinstance(raw_proposals, list):
        raise ValueError("generation proposals must be an array")
    proposals: list[tuple[ProposalKind, dict[str, Any], tuple[str, ...]]] = []
    for raw in raw_proposals:
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "payload",
            "evidence_utterance_ids",
        }:
            raise ValueError("generation proposal fields are invalid")
        if not isinstance(raw["payload"], dict) or not isinstance(
            raw["evidence_utterance_ids"], list
        ):
            raise ValueError("generation proposal values are invalid")
        proposals.append(
            (
                ProposalKind(raw["kind"]),
                raw["payload"],
                tuple(raw["evidence_utterance_ids"]),
            )
        )
    return GenerationSubmission(
        layer=KnowledgeLayer(body["layer"]),
        producer=body["producer"],
        producer_version=body["producer_version"],
        model=body["model"],
        prompt_version=body["prompt_version"],
        extractor_version=body["extractor_version"],
        input_scope=body["input_scope"],
        proposals=tuple(proposals),
    )


def _reminder_generation_submission(
    body: dict[str, Any],
) -> ReminderGenerationSubmission:
    required = {
        "producer",
        "producer_version",
        "model",
        "prompt_version",
        "extractor_version",
        "input_scope",
        "intents",
    }
    if set(body) != required:
        raise ValueError("reminder generation submission fields are invalid")
    for name in (
        "producer",
        "producer_version",
        "model",
        "prompt_version",
        "extractor_version",
    ):
        if not isinstance(body[name], str) or not body[name].strip():
            raise ValueError(f"reminder generation {name} is required")
    if not isinstance(body["input_scope"], dict) or not isinstance(
        body["intents"], list
    ):
        raise ValueError("reminder generation scope and intents are invalid")
    intents: list[ReminderIntent] = []
    intent_required = {
        "operation",
        "session_id",
        "actor_person_id",
        "commitment_direction",
        "confidence",
        "evidence_utterance_ids",
        "needs_confirmation",
    }
    intent_allowed = intent_required | {
        "title",
        "related_person_ids",
        "scheduled_at",
        "location",
        "target_event_id",
        "expected_revision",
        "reason",
    }
    for raw in body["intents"]:
        if (
            not isinstance(raw, dict)
            or set(raw) - intent_allowed
            or not intent_required.issubset(raw)
        ):
            raise ValueError("reminder intent fields are invalid")
        related = raw.get("related_person_ids", [])
        evidence = raw["evidence_utterance_ids"]
        confidence = raw["confidence"]
        revision = raw.get("expected_revision", 0)
        if (
            not isinstance(related, list)
            or not all(isinstance(value, str) for value in related)
            or not isinstance(evidence, list)
            or not all(isinstance(value, str) for value in evidence)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not isinstance(raw["needs_confirmation"], bool)
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            raise ValueError("reminder intent values are invalid")
        intents.append(
            ReminderIntent(
                operation=ReminderOperation(raw["operation"]),
                session_id=raw["session_id"],
                title=raw.get("title"),
                actor_person_id=raw["actor_person_id"],
                commitment_direction=CommitmentDirection(
                    raw["commitment_direction"]
                ),
                related_person_ids=tuple(related),
                scheduled_at=(
                    _reminder_datetime(raw["scheduled_at"])
                    if raw.get("scheduled_at") is not None
                    else None
                ),
                location=raw.get("location"),
                confidence=float(confidence),
                evidence_utterance_ids=tuple(evidence),
                needs_confirmation=raw["needs_confirmation"],
                target_event_id=raw.get("target_event_id"),
                expected_revision=revision,
                reason=raw.get("reason"),
            )
        )
    return ReminderGenerationSubmission(
        producer=body["producer"],
        producer_version=body["producer_version"],
        model=body["model"],
        prompt_version=body["prompt_version"],
        extractor_version=body["extractor_version"],
        input_scope=body["input_scope"],
        intents=tuple(intents),
    )


def _reminder_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("reminder timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("reminder timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("reminder timestamp requires a timezone")
    return parsed


def _parse_range(value: str | None, size: int) -> tuple[int, int]:
    if size < 1:
        raise ValueError("media is empty")
    if value is None:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise ValueError("media range is invalid")
    if not match.group(1):
        length = int(match.group(2))
        if length < 1:
            raise ValueError("media range is invalid")
        return max(0, size - length), size - 1
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else size - 1
    if start < 0 or start >= size or end < start:
        raise ValueError("media range is outside the asset")
    return start, min(end, size - 1)
