from __future__ import annotations

import sqlite3
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from allday_asr.v3.application.people import SpeakerIdentityService
from allday_asr.v3.ports.repositories import UnitOfWork

from .importer import _database_fingerprint


UnitOfWorkFactory = Callable[[], UnitOfWork]
_ACTOR = "system:legacy-v2-human-speaker-migration"
_KNOWN_LABELS = {
    "mother": ("mother", "母亲"),
    "母亲": ("mother", "母亲"),
    "妈妈": ("mother", "母亲"),
    "我妈": ("mother", "母亲"),
    "father": ("father", "父亲"),
    "父亲": ("father", "父亲"),
    "爸爸": ("father", "父亲"),
    "我爸": ("father", "父亲"),
}


@dataclass(frozen=True)
class LegacyIdentityWindow:
    legacy_session_id: int
    start_ms: int
    end_ms: int
    label: str
    source_ref: str


@dataclass(frozen=True)
class LegacySpeakerIdentityBackfillResult:
    source_database_sha256: str
    source_namespace: str
    source_window_count: int
    eligible_window_count: int
    skipped_short_window_count: int
    skipped_conflicting_window_count: int
    created_seed_count: int
    existing_seed_count: int
    preserved_review_count: int
    unmapped_session_groups: tuple[str, ...]
    unmapped_person_labels: tuple[str, ...]
    seeds: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_database_sha256": self.source_database_sha256,
            "source_namespace": self.source_namespace,
            "source_window_count": self.source_window_count,
            "eligible_window_count": self.eligible_window_count,
            "skipped_short_window_count": self.skipped_short_window_count,
            "skipped_conflicting_window_count": self.skipped_conflicting_window_count,
            "created_seed_count": self.created_seed_count,
            "existing_seed_count": self.existing_seed_count,
            "preserved_review_count": self.preserved_review_count,
            "unmapped_session_groups": list(self.unmapped_session_groups),
            "unmapped_person_labels": list(self.unmapped_person_labels),
            "seeds": [dict(value) for value in self.seeds],
        }


class LegacySpeakerIdentityBackfill:
    """One-way import of trusted V2 identity windows into V3-native voice seeds."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        people: SpeakerIdentityService,
    ) -> None:
        self._uow_factory = uow_factory
        self._people = people

    def execute(self, source_database: Path) -> LegacySpeakerIdentityBackfillResult:
        source = source_database.resolve(strict=True)
        before_sha256 = _database_fingerprint(source)
        connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            windows = _identity_windows(connection)
        finally:
            connection.close()
        if _database_fingerprint(source) != before_sha256:
            raise RuntimeError("legacy V2 database changed while speaker labels were read")

        with self._uow_factory() as uow:
            namespace = uow.legacy_imports.latest_namespace(str(source))
        if namespace is None:
            raise ValueError("legacy V2 database has not been imported into this V3 state")

        people_by_name = {
            str(value["display_name"]): str(value["person_id"])
            for value in self._people.list_people()
            if value["kind"] == "known"
        }
        eligible, short_count, conflicting_count = _eligible_windows(windows)
        grouped: dict[tuple[str, int], list[LegacyIdentityWindow]] = defaultdict(list)
        for window in eligible:
            normalized = _known_label(window.label)
            if normalized is not None:
                grouped[(normalized[0], window.legacy_session_id)].append(window)

        seeds: list[dict[str, Any]] = []
        unmapped_sessions: list[str] = []
        unmapped_people: set[str] = set()
        created = 0
        existing = 0
        preserved = 0
        for (label, legacy_session_id), values in sorted(grouped.items()):
            display_name = _KNOWN_LABELS[label][1]
            person_id = people_by_name.get(display_name)
            if person_id is None:
                unmapped_people.add(label)
                continue
            legacy_ref = f"v2:{namespace}:recording_sessions:{legacy_session_id}"
            with self._uow_factory() as uow:
                session = uow.catalog.find_session_by_legacy_ref(legacy_ref)
            if session is None:
                unmapped_sessions.append(f"{label}:{legacy_session_id}")
                continue
            merged = _merge_windows(values)
            source_ref = (
                f"v2:{namespace}:confirmed-speaker-enrollment:"
                f"{label}:{legacy_session_id}"
            )
            result = self._people.enroll_confirmed_windows(
                person_id,
                session.session_id,
                tuple((value.start_ms, value.end_ms) for value in merged),
                source_ref=source_ref,
                display_label=f"历史人工声纹 · {display_name}",
                actor=_ACTOR,
                note=(
                    f"V2 人工身份区间迁移；label={label}; "
                    f"legacy_session_id={legacy_session_id}; "
                    f"source_sha256={before_sha256}; windows={len(merged)}"
                ),
            )
            state = str(result["state"])
            created += int(state == "created")
            existing += int(state == "existing")
            preserved += int(state.startswith("preserved_"))
            seeds.append(
                {
                    "state": state,
                    "label": label,
                    "display_name": display_name,
                    "legacy_session_id": legacy_session_id,
                    "session_id": session.session_id,
                    "window_count": len(merged),
                    "prototype_id": result["prototype_id"],
                    "cluster_id": result["cluster_id"],
                }
            )

        return LegacySpeakerIdentityBackfillResult(
            source_database_sha256=before_sha256,
            source_namespace=namespace,
            source_window_count=len(windows),
            eligible_window_count=len(eligible),
            skipped_short_window_count=short_count,
            skipped_conflicting_window_count=conflicting_count,
            created_seed_count=created,
            existing_seed_count=existing,
            preserved_review_count=preserved,
            unmapped_session_groups=tuple(unmapped_sessions),
            unmapped_person_labels=tuple(sorted(unmapped_people)),
            seeds=tuple(seeds),
        )


def _identity_windows(connection: sqlite3.Connection) -> tuple[LegacyIdentityWindow, ...]:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    values: list[LegacyIdentityWindow] = []
    if "manual_identity_annotations" in tables:
        for row in connection.execute(
            """
            SELECT id, session_id, session_start_ms, session_end_ms, identity_label
            FROM manual_identity_annotations
            WHERE status = 'active'
            ORDER BY session_id, session_start_ms, id
            """
        ):
            values.append(
                LegacyIdentityWindow(
                    int(row["session_id"]),
                    int(row["session_start_ms"]),
                    int(row["session_end_ms"]),
                    _label(row["identity_label"]),
                    f"manual_identity_annotations:{int(row['id'])}",
                )
            )
    if {
        "segment_identity_annotations",
        "speech_segments",
        "recording_sessions",
    }.issubset(tables):
        for row in connection.execute(
            """
            SELECT annotation.id, session.id AS session_id,
              segment.start_ms, segment.end_ms, annotation.identity_label
            FROM segment_identity_annotations annotation
            JOIN speech_segments segment ON segment.id = annotation.segment_id
            JOIN recording_sessions session
              ON session.legacy_recording_id = segment.recording_id
            WHERE lower(annotation.annotation_source) = 'human'
              AND lower(annotation.confidence) = 'confirmed'
            ORDER BY session.id, segment.start_ms, annotation.id
            """
        ):
            values.append(
                LegacyIdentityWindow(
                    int(row["session_id"]),
                    int(row["start_ms"]),
                    int(row["end_ms"]),
                    _label(row["identity_label"]),
                    f"segment_identity_annotations:{int(row['id'])}",
                )
            )
    if {
        "v2d1_identity_labels",
        "v2d1_candidate_reviews",
        "processing_runs",
    }.issubset(tables):
        for row in connection.execute(
            """
            SELECT label.id, run.session_id, review.session_start_ms,
              review.session_end_ms, label.identity_label
            FROM v2d1_identity_labels label
            JOIN v2d1_candidate_reviews review
              ON review.run_id = label.run_id
             AND review.candidate_id = label.candidate_id
            JOIN processing_runs run ON run.id = label.run_id
            WHERE review.status = 'confirmed_speech' AND run.session_id IS NOT NULL
            ORDER BY run.session_id, review.session_start_ms, label.id
            """
        ):
            values.append(
                LegacyIdentityWindow(
                    int(row["session_id"]),
                    int(row["session_start_ms"]),
                    int(row["session_end_ms"]),
                    _label(row["identity_label"]),
                    f"v2d1_identity_labels:{int(row['id'])}",
                )
            )
    return tuple(values)


def _eligible_windows(
    windows: tuple[LegacyIdentityWindow, ...],
) -> tuple[tuple[LegacyIdentityWindow, ...], int, int]:
    target_windows = [value for value in windows if _known_label(value.label) is not None]
    short_count = 0
    conflicting_count = 0
    eligible: list[LegacyIdentityWindow] = []
    by_session: dict[int, list[LegacyIdentityWindow]] = defaultdict(list)
    for value in windows:
        by_session[value.legacy_session_id].append(value)
    for value in target_windows:
        if value.end_ms - value.start_ms < 800:
            short_count += 1
            continue
        target = _known_label(value.label)
        conflicts = any(
            other.source_ref != value.source_ref
            and _canonical_label(other.label) != target[0]
            and other.start_ms < value.end_ms
            and other.end_ms > value.start_ms
            for other in by_session[value.legacy_session_id]
        )
        if conflicts:
            conflicting_count += 1
            continue
        eligible.append(value)
    return tuple(eligible), short_count, conflicting_count


def _merge_windows(values: list[LegacyIdentityWindow]) -> tuple[LegacyIdentityWindow, ...]:
    ordered = sorted(values, key=lambda value: (value.start_ms, value.end_ms))
    merged: list[LegacyIdentityWindow] = []
    for value in ordered:
        if merged and value.start_ms <= merged[-1].end_ms:
            previous = merged[-1]
            merged[-1] = LegacyIdentityWindow(
                previous.legacy_session_id,
                previous.start_ms,
                max(previous.end_ms, value.end_ms),
                previous.label,
                f"{previous.source_ref},{value.source_ref}",
            )
        else:
            merged.append(value)
    return tuple(merged)


def _known_label(value: str) -> tuple[str, str] | None:
    return _KNOWN_LABELS.get(_label(value))


def _canonical_label(value: str) -> str:
    known = _known_label(value)
    return known[0] if known is not None else _label(value)


def _label(value: object) -> str:
    return str(value).strip().casefold()


__all__ = [
    "LegacyIdentityWindow",
    "LegacySpeakerIdentityBackfill",
    "LegacySpeakerIdentityBackfillResult",
]
