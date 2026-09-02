from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from allday_asr.application.semantic.evidence import (
    ConversationEvidenceSettings,
    build_conversation_evidence,
)
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.knowledge import (
    DerivationDependency,
    EventCurrentState,
    EventKind,
    EventOperation,
    EventOperationKind,
    EventStatus,
    GenerationRecord,
    GenerationStatus,
    KnowledgeLayer,
    ProposalKind,
    ProposalStatus,
    StructuredProposal,
)
from allday_asr.v3.domain.processing import SpeakerTrack, Utterance


_SEMANTIC_TABLES = {
    "asr_hypotheses",
    "asr_alignment_tokens",
    "asr_token_sources",
    "semantic_exchanges",
    "semantic_candidates",
}
_IDENTITY_NAMES = {
    "self": ("我", ("本人", "self"), ()),
    "mother": ("母亲", ("妈妈", "mother"), ("母亲",)),
    "father": ("父亲", ("爸爸", "father"), ("父亲",)),
}


@dataclass(frozen=True)
class LegacyPersonProjection:
    person_id: str
    display_name: str
    kind: str
    aliases: tuple[str, ...]
    relationship_labels: tuple[str, ...]
    created_at: datetime
    legacy_ref: str


@dataclass
class LegacySemanticProjection:
    persons: list[LegacyPersonProjection] = field(default_factory=list)
    speaker_tracks: list[SpeakerTrack] = field(default_factory=list)
    utterances: list[Utterance] = field(default_factory=list)
    generations: list[GenerationRecord] = field(default_factory=list)
    proposals: list[StructuredProposal] = field(default_factory=list)
    event_operations: list[EventOperation] = field(default_factory=list)
    event_states: list[EventCurrentState] = field(default_factory=list)
    event_evidence: list[tuple[str, str, int, str, int, datetime]] = field(
        default_factory=list
    )
    dependencies: list[DerivationDependency] = field(default_factory=list)


def prepare_legacy_semantic_projection(
    connection: sqlite3.Connection,
    namespace: str,
    sessions: Mapping[int, sqlite3.Row],
    *,
    now: datetime,
) -> LegacySemanticProjection:
    """Project immutable V2 evidence into V3 without rerunning any model."""

    tables = _table_names(connection)
    if not _SEMANTIC_TABLES.issubset(tables):
        return LegacySemanticProjection()
    selected = _selected_exchanges(connection)
    if not selected:
        return LegacySemanticProjection()

    projection = LegacySemanticProjection()
    label_people = _prepare_people(connection, namespace, selected, projection, now=now)
    utterances_by_session: dict[int, list[Utterance]] = defaultdict(list)
    annotations: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for annotation in _identity_annotations(connection):
        annotations[int(annotation["session_id"])].append(annotation)
    source_artifact_id = stable_ulid(
        _legacy_ref(namespace, "asr_alignment_tokens_snapshot", "all")
    )

    for legacy_session_id, exchange in selected.items():
        session = sessions.get(legacy_session_id)
        if session is None:
            continue
        tokens = _semantic_tokens(connection, exchange)
        if not tokens:
            continue
        conversations, excluded = build_conversation_evidence(
            tokens,
            (),
            settings=ConversationEvidenceSettings(),
        )
        raw_utterances = sorted(
            (
                dict(value)
                for conversation in (*conversations, *excluded)
                for value in conversation["utterances"]
                if str(value.get("text") or "").strip()
            ),
            key=lambda value: (
                int(value["start_ms"]),
                int(value["end_ms"]),
                str(value["key"]),
            ),
        )
        if not raw_utterances:
            continue
        v3_session_id = stable_ulid(
            _legacy_ref(namespace, "recording_sessions", legacy_session_id)
        )
        legacy_run_id = int(exchange["run_id"])
        v3_run_id = stable_ulid(
            _legacy_ref(namespace, "processing_runs", legacy_run_id)
        )
        captured_start = _legacy_datetime(
            session["recorded_at"], str(session["timezone"])
        )
        created_at = _legacy_datetime(exchange["created_at"], "UTC", fallback=now)
        speaker_ids: dict[str, str] = {}
        for label in sorted(
            {str(value.get("speaker") or "unassigned") for value in raw_utterances}
        ):
            track_ref = _legacy_ref(
                namespace,
                "semantic_speaker_tracks",
                f"{legacy_run_id}:{label}",
            )
            track_id = stable_ulid(track_ref)
            speaker_ids[label] = track_id
            projection.speaker_tracks.append(
                SpeakerTrack(
                    speaker_track_id=track_id,
                    session_id=v3_session_id,
                    run_id=v3_run_id,
                    label=label,
                    source_artifact_id=source_artifact_id,
                    created_at=created_at,
                )
            )
        for ordinal, value in enumerate(raw_utterances):
            start_ms = int(value["start_ms"])
            end_ms = int(value["end_ms"])
            label = str(value.get("speaker") or "unassigned")
            identity, identity_evidence = _identity(
                annotations.get(legacy_session_id, ()),
                diarization_run_id=_optional_int(exchange["diarization_run_id"]),
                speaker_label=label,
                start_ms=start_ms,
                end_ms=end_ms,
            )
            utterance_ref = _legacy_ref(
                namespace,
                "semantic_utterances",
                f"{legacy_run_id}:{value['key']}",
            )
            evidence = dict(value.get("evidence") or {})
            evidence["legacy"] = {
                "semantic_run_id": legacy_run_id,
                "asr_run_id": int(exchange["asr_run_id"]),
                "diarization_run_id": _optional_int(exchange["diarization_run_id"]),
                "utterance_key": str(value["key"]),
                "speaker_kind": str(value.get("speaker_kind") or "none"),
            }
            utterance = Utterance(
                utterance_id=stable_ulid(utterance_ref),
                session_id=v3_session_id,
                run_id=v3_run_id,
                source_artifact_id=source_artifact_id,
                speaker_track_id=speaker_ids[label],
                original_speaker_track_id=speaker_ids[label],
                ordinal=ordinal,
                start_ms=start_ms,
                end_ms=end_ms,
                start_at=captured_start + timedelta(milliseconds=start_ms),
                end_at=captured_start + timedelta(milliseconds=end_ms),
                text=str(value["text"]),
                original_text=str(value["text"]),
                identity=identity,
                original_identity=identity,
                identity_evidence=identity_evidence,
                evidence=evidence,
                revision=1,
                status="active",
                created_at=created_at,
                updated_at=created_at,
            )
            projection.utterances.append(utterance)
            utterances_by_session[legacy_session_id].append(utterance)

        _prepare_events(
            connection,
            namespace,
            legacy_session_id,
            exchange,
            utterances_by_session[legacy_session_id],
            label_people,
            projection,
            now=now,
        )
    return projection


def _prepare_people(
    connection: sqlite3.Connection,
    namespace: str,
    exchanges: Mapping[int, sqlite3.Row],
    projection: LegacySemanticProjection,
    *,
    now: datetime,
) -> dict[str, str]:
    by_label: dict[str, str] = {}
    seen: set[str] = set()
    if "person_profiles" in _table_names(connection):
        required = {"id", "display_name", "profile_type", "created_at"}
        if required.issubset(_columns(connection, "person_profiles")):
            for row in connection.execute("SELECT * FROM person_profiles ORDER BY id"):
                legacy_ref = _legacy_ref(namespace, "person_profiles", int(row["id"]))
                person_id = stable_ulid(legacy_ref)
                profile_type = str(row["profile_type"])
                kind = "self" if profile_type == "self" else "known"
                projection.persons.append(
                    LegacyPersonProjection(
                        person_id=person_id,
                        display_name=str(row["display_name"]),
                        kind=kind,
                        aliases=(),
                        relationship_labels=(),
                        created_at=_legacy_datetime(
                            row["created_at"], "UTC", fallback=now
                        ),
                        legacy_ref=legacy_ref,
                    )
                )
                seen.add(person_id)
                if kind == "self":
                    by_label["self"] = person_id

    labels = {
        str(row["identity_label"]).strip().casefold()
        for row in _identity_annotations(connection)
        if int(row["session_id"]) in exchanges
    }
    for label in sorted(labels & _IDENTITY_NAMES.keys()):
        if label in by_label:
            continue
        display_name, aliases, relationships = _IDENTITY_NAMES[label]
        legacy_ref = _legacy_ref(namespace, "identity_labels", label)
        person_id = stable_ulid(legacy_ref)
        by_label[label] = person_id
        if person_id in seen:
            continue
        projection.persons.append(
            LegacyPersonProjection(
                person_id=person_id,
                display_name=display_name,
                kind="self" if label == "self" else "known",
                aliases=aliases,
                relationship_labels=relationships,
                created_at=now,
                legacy_ref=legacy_ref,
            )
        )
        seen.add(person_id)
    return by_label


def _prepare_events(
    connection: sqlite3.Connection,
    namespace: str,
    legacy_session_id: int,
    exchange: sqlite3.Row,
    utterances: list[Utterance],
    label_people: Mapping[str, str],
    projection: LegacySemanticProjection,
    *,
    now: datetime,
) -> None:
    candidates = list(
        connection.execute(
            """
            SELECT * FROM semantic_candidates
            WHERE run_id = ? AND candidate_type != 'daily_summary'
            ORDER BY session_start_ms, id
            """,
            (int(exchange["run_id"]),),
        )
    )
    grounded = [
        (candidate, _overlapping_utterances(candidate, utterances))
        for candidate in candidates
    ]
    grounded = [(candidate, evidence) for candidate, evidence in grounded if evidence]
    if not grounded:
        return

    legacy_run_id = int(exchange["run_id"])
    generation_ref = _legacy_ref(namespace, "semantic_generations", legacy_run_id)
    generation_id = stable_ulid(generation_ref)
    created_at = _legacy_datetime(exchange["created_at"], "UTC", fallback=now)
    completed_at = created_at
    input_sha256 = _digest(
        exchange["request_sha256"],
        {
            "source_namespace": namespace,
            "semantic_run_id": legacy_run_id,
            "candidate_ids": [int(row[0]["id"]) for row in grounded],
        },
    )
    projection.generations.append(
        GenerationRecord(
            generation_id=generation_id,
            layer=KnowledgeLayer.EVENT,
            producer=str(exchange["provider"] or "legacy_v2"),
            producer_version=str(exchange["legacy_pipeline_version"] or "legacy"),
            model=str(exchange["model"] or "legacy_v2"),
            prompt_version=str(exchange["request_format"] or "legacy"),
            extractor_version=str(exchange["response_format"] or "legacy"),
            input_scope={
                "legacy_source_namespace": namespace,
                "legacy_session_id": legacy_session_id,
                "legacy_semantic_run_id": legacy_run_id,
            },
            input_sha256=input_sha256,
            generation_number=1,
            status=GenerationStatus.SUCCEEDED,
            created_at=created_at,
            completed_at=completed_at,
        )
    )
    v3_session_id = stable_ulid(
        _legacy_ref(namespace, "recording_sessions", legacy_session_id)
    )
    for candidate, evidence in grounded:
        candidate_id = int(candidate["id"])
        event_ref = _legacy_ref(namespace, "semantic_candidates", candidate_id)
        event_id = stable_ulid(event_ref)
        proposal_id = stable_ulid(event_ref, "proposal")
        operation_id = stable_ulid(event_ref, "operation", 1)
        candidate_created_at = _legacy_datetime(
            candidate["created_at"], "UTC", fallback=created_at
        )
        event_kind = _event_kind(str(candidate["candidate_type"]))
        payload = _event_payload(candidate, label_people)
        evidence_ids = tuple(value.utterance_id for value in evidence)
        projection.proposals.append(
            StructuredProposal(
                proposal_id=proposal_id,
                generation_id=generation_id,
                kind=ProposalKind.EVENT_OPERATION,
                payload={
                    "event_id": event_id,
                    "session_id": v3_session_id,
                    "event_kind": event_kind.value,
                    "operation": EventOperationKind.CREATE.value,
                    "payload": payload,
                },
                evidence_utterance_ids=evidence_ids,
                status=ProposalStatus.ACCEPTED,
                created_at=candidate_created_at,
                resolved_at=candidate_created_at,
                resolved_by="legacy_v2_import",
                resolution_reason="immutable legacy semantic candidate",
            )
        )
        projection.event_operations.append(
            EventOperation(
                operation_id=operation_id,
                event_id=event_id,
                session_id=v3_session_id,
                event_kind=event_kind,
                operation=EventOperationKind.CREATE,
                event_revision=1,
                payload=payload,
                actor="system:legacy_v2_import",
                generation_id=generation_id,
                proposal_id=proposal_id,
                created_at=candidate_created_at,
            )
        )
        projection.event_states.append(
            EventCurrentState(
                event_id=event_id,
                session_id=v3_session_id,
                event_kind=event_kind,
                status=EventStatus.ACTIVE,
                revision=1,
                payload=payload,
                latest_operation_id=operation_id,
                created_at=candidate_created_at,
                updated_at=candidate_created_at,
            )
        )
        for utterance in evidence:
            projection.event_evidence.append(
                (
                    stable_ulid(event_ref, "evidence", utterance.utterance_id),
                    event_id,
                    1,
                    utterance.utterance_id,
                    utterance.revision,
                    candidate_created_at,
                )
            )
            projection.dependencies.append(
                DerivationDependency(
                    dependent_type="event",
                    dependent_id=event_id,
                    dependent_revision=1,
                    input_type="utterance",
                    input_id=utterance.utterance_id,
                    input_revision=utterance.revision,
                    created_at=candidate_created_at,
                )
            )


def _selected_exchanges(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    required = {
        "run_id",
        "session_id",
        "asr_run_id",
        "diarization_run_id",
        "provider",
        "model",
        "request_format",
        "response_format",
        "request_sha256",
        "created_at",
    }
    if not required.issubset(_columns(connection, "semantic_exchanges")):
        return {}
    selected: dict[int, sqlite3.Row] = {}
    rows = connection.execute(
        """
        SELECT exchange.*, run.pipeline_version AS legacy_pipeline_version
        FROM semantic_exchanges exchange
        JOIN processing_runs run ON run.id = exchange.run_id
        WHERE run.status = 'completed'
        ORDER BY exchange.session_id, exchange.run_id
        """
    )
    for row in rows:
        selected[int(row["session_id"])] = row
    return selected


def _semantic_tokens(
    connection: sqlite3.Connection, exchange: sqlite3.Row
) -> list[dict[str, Any]]:
    asr_run_id = int(exchange["asr_run_id"])
    sources: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT source.*, token.session_start_ms, token.session_end_ms
        FROM asr_token_sources source
        JOIN asr_alignment_tokens token ON token.id = source.token_id
        JOIN asr_hypotheses hypothesis ON hypothesis.id = token.hypothesis_id
        WHERE hypothesis.run_id = ? AND hypothesis.hypothesis_role = 'primary'
          AND token.kept_in_core = 1
        ORDER BY token.session_start_ms, token.session_end_ms,
                 token.id, source.position
        """,
        (asr_run_id,),
    ):
        sources[int(row["token_id"])].append(
            {
                "source_object_id": int(row["source_object_id"]),
                "source_instance_id": (
                    int(row["source_instance_id"])
                    if "source_instance_id" in row.keys()
                    and row["source_instance_id"] is not None
                    else None
                ),
                "source_sha256": str(row["source_sha256"]),
                "source_start_ms": int(row["source_start_ms"]),
                "source_end_ms": int(row["source_end_ms"]),
            }
        )
    attributions: dict[int, list[sqlite3.Row]] = defaultdict(list)
    diarization_run_id = _optional_int(exchange["diarization_run_id"])
    if diarization_run_id is not None and "token_speaker_attributions" in _table_names(
        connection
    ):
        for row in connection.execute(
            """
            SELECT * FROM token_speaker_attributions
            WHERE run_id = ? ORDER BY token_id, rank
            """,
            (diarization_run_id,),
        ):
            attributions[int(row["token_id"])].append(row)
    tokens: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT token.* FROM asr_alignment_tokens token
        JOIN asr_hypotheses hypothesis ON hypothesis.id = token.hypothesis_id
        WHERE hypothesis.run_id = ? AND hypothesis.hypothesis_role = 'primary'
          AND token.kept_in_core = 1
        ORDER BY token.session_start_ms, token.session_end_ms, token.id
        """,
        (asr_run_id,),
    ):
        token_id = int(row["id"])
        source_refs = sources.get(token_id, [])
        if not source_refs:
            continue
        token_attributions = attributions.get(token_id, [])
        primary = token_attributions[0] if token_attributions else None
        tokens.append(
            {
                "id": token_id,
                "text": str(row["text"]),
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "speaker": primary["speaker_label"] if primary else None,
                "speaker_kind": (
                    str(primary["attribution_kind"]) if primary else "none"
                ),
                "speaker_confidence": (
                    float(primary["confidence"])
                    if primary is not None and primary["confidence"] is not None
                    else None
                ),
                "has_overlap": any(
                    str(value["attribution_kind"]) == "overlap"
                    for value in token_attributions
                ),
                "source_refs": source_refs,
            }
        )
    return tokens


def _identity_annotations(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    required = {
        "session_id",
        "diarization_run_id",
        "session_start_ms",
        "session_end_ms",
        "identity_label",
        "anonymous_speaker_label",
        "status",
    }
    if "manual_identity_annotations" not in _table_names(connection):
        return []
    if not required.issubset(_columns(connection, "manual_identity_annotations")):
        return []
    return list(
        connection.execute(
            """
            SELECT * FROM manual_identity_annotations
            WHERE status = 'active'
            ORDER BY session_id, diarization_run_id, session_start_ms, id
            """
        )
    )


def _identity(
    annotations: tuple[sqlite3.Row, ...] | list[sqlite3.Row],
    *,
    diarization_run_id: int | None,
    speaker_label: str,
    start_ms: int,
    end_ms: int,
) -> tuple[SelfIdentity, dict[str, Any]]:
    matches = [
        row
        for row in annotations
        if (
            diarization_run_id is None
            or int(row["diarization_run_id"]) == diarization_run_id
        )
        and str(row["anonymous_speaker_label"]).casefold() == speaker_label.casefold()
        and int(row["session_start_ms"]) < end_ms
        and int(row["session_end_ms"]) > start_ms
    ]
    labels = sorted({str(row["identity_label"]).strip().casefold() for row in matches})
    if labels == ["self"]:
        decision = SelfIdentity.SELF
        reason = "legacy_human_self_window"
    elif labels and "self" not in labels:
        decision = SelfIdentity.NOT_SELF
        reason = "legacy_human_not_self_window"
    else:
        decision = SelfIdentity.UNKNOWN
        reason = "no_unambiguous_legacy_human_window"
    return decision, {
        "source": "legacy_manual_identity_annotations",
        "decision": decision.value,
        "reason": reason,
        "policy_version": "v3-legacy-semantic-backfill.1",
        "human_reviewed": bool(matches),
        "legacy_identity_labels": labels,
        "legacy_annotation_ids": [int(row["id"]) for row in matches],
    }


def _event_kind(candidate_type: str) -> EventKind:
    return {
        "action": EventKind.TASK,
        "fact": EventKind.PERSON_FACT,
        "event": EventKind.IMPORTANT_EXPERIENCE,
    }.get(candidate_type, EventKind.IMPORTANT_EXPERIENCE)


def _event_payload(
    candidate: sqlite3.Row, label_people: Mapping[str, str]
) -> dict[str, Any]:
    title = str(candidate["title"] or "").strip()
    body = str(candidate["body"] or "").strip()
    haystack = f"{title}\n{body}".casefold()
    mentioned: list[str] = []
    actor_person_id: str | None = None
    suffix = title.rsplit("·", 1)[-1].strip().casefold() if "·" in title else ""
    for label, person_id in label_people.items():
        display_name, aliases, _relationships = _IDENTITY_NAMES.get(
            label, (label, (), ())
        )
        terms = {
            label,
            display_name.casefold(),
            *(value.casefold() for value in aliases),
        }
        if suffix == label or any(term and term in haystack for term in terms):
            mentioned.append(person_id)
        if suffix == label:
            actor_person_id = person_id
    payload: dict[str, Any] = {
        "title": title,
        "summary": body,
        "topics": (
            [title] if title and not title.startswith(("陈述 ·", "行动候选 ·")) else []
        ),
        "related_person_ids": list(dict.fromkeys(mentioned)),
        "legacy": {
            "source": "semantic_candidates",
            "candidate_id": int(candidate["id"]),
            "candidate_key": str(candidate["candidate_key"]),
            "candidate_type": str(candidate["candidate_type"]),
            "session_start_ms": int(candidate["session_start_ms"]),
            "session_end_ms": int(candidate["session_end_ms"]),
        },
    }
    if candidate["confidence"] is not None:
        payload["confidence"] = float(candidate["confidence"])
    if actor_person_id is not None:
        payload["actor_person_id"] = actor_person_id
        payload["person_id"] = actor_person_id
    return payload


def _overlapping_utterances(
    candidate: sqlite3.Row, utterances: list[Utterance]
) -> list[Utterance]:
    start_ms = int(candidate["session_start_ms"])
    end_ms = int(candidate["session_end_ms"])
    return [
        value
        for value in utterances
        if value.start_ms < end_ms and value.end_ms > start_ms
    ]


def _digest(value: object, fallback: object) -> str:
    candidate = str(value or "").casefold()
    if len(candidate) == 64:
        try:
            int(candidate, 16)
        except ValueError:
            pass
        else:
            return candidate
    payload = json.dumps(
        fallback,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _legacy_ref(namespace: str, table: str, key: object) -> str:
    return f"v2:{namespace}:{table}:{key}"


def _legacy_datetime(
    value: object,
    timezone_name: str,
    *,
    fallback: datetime | None = None,
) -> datetime:
    if value is None:
        if fallback is None:
            raise ValueError("legacy timestamp is missing")
        return fallback.astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
    return parsed.astimezone(timezone.utc)


def _optional_int(value: object) -> int | None:
    return int(value) if value is not None else None


__all__ = [
    "LegacyPersonProjection",
    "LegacySemanticProjection",
    "prepare_legacy_semantic_projection",
]
