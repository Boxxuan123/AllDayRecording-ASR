from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from allday_asr.domain.hashing import canonical_json_sha256


LEGACY_LABEL_MIGRATION_FORMAT = (
    "AllDayRecording V3.1 legacy label migration v1"
)
LEGACY_LABEL_MIGRATION_POLICY = "v3.1-label-migration.2"
LEGACY_LABEL_MIGRATION_REVISION = 2
MIN_IDENTITY_WINDOW_MS = 2_000
MAX_IDENTITY_WINDOW_MS = 5_000
CHUNK_INTERVAL_MS = 300_000
SEAM_WINDOW_MS = 5_000
MIN_SELF_WINDOWS = 20
MIN_NOT_SELF_WINDOWS = 50
MIN_REVIEWED_SEAMS = 10
MIN_REFERENCE_UTTERANCES = 30
MIN_OVERLAP_UTTERANCES = 5


@dataclass(frozen=True)
class LegacyLabelMigrationSummary:
    bundle_path: Path
    receipt_path: Path
    report_path: Path
    source_database_sha256: str
    bundle_sha256: str
    receipt_sha256: str
    assessment: dict[str, Any]
    counts: dict[str, int]


def migrate_legacy_labels(
    source_database: Path,
    *,
    output_dir: Path,
) -> LegacyLabelMigrationSummary:
    source = source_database.resolve(strict=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    source_sha256 = _database_fingerprint(source)
    connection = _open_read_only(source)
    try:
        body = _migration_body(connection, source_sha256)
    finally:
        connection.close()
    if _database_fingerprint(source) != source_sha256:
        raise RuntimeError("legacy database changed while labels were migrated")

    bundle_sha256 = canonical_json_sha256(body)
    bundle = {**body, "bundle_sha256": bundle_sha256}
    stem = (
        f"legacy-v2-labels-r{LEGACY_LABEL_MIGRATION_REVISION}-"
        f"{source_sha256[:12]}"
    )
    bundle_path = output_dir / f"{stem}.json"
    receipt_path = output_dir / f"{stem}.receipt.json"
    report_path = output_dir / f"{stem}.assessment.md"
    counts = dict(body["counts"])
    assessment = dict(body["assessment"])
    receipt_body = {
        "format": LEGACY_LABEL_MIGRATION_FORMAT,
        "receipt_version": 1,
        "policy_version": LEGACY_LABEL_MIGRATION_POLICY,
        "source_database_sha256": source_sha256,
        "bundle_sha256": bundle_sha256,
        "counts": counts,
        "assessment": assessment,
    }
    receipt_sha256 = canonical_json_sha256(receipt_body)
    receipt = {**receipt_body, "receipt_sha256": receipt_sha256}
    _publish_json(bundle_path, bundle)
    _publish_json(receipt_path, receipt)
    _publish_text(report_path, _render_assessment(assessment, counts))
    return LegacyLabelMigrationSummary(
        bundle_path=bundle_path,
        receipt_path=receipt_path,
        report_path=report_path,
        source_database_sha256=source_sha256,
        bundle_sha256=bundle_sha256,
        receipt_sha256=receipt_sha256,
        assessment=assessment,
        counts=counts,
    )


def _migration_body(
    connection: sqlite3.Connection, source_sha256: str
) -> dict[str, Any]:
    tables = _table_names(connection)
    required = {"schema_migrations", "recording_sessions"}
    missing = sorted(required - tables)
    if missing:
        raise ValueError(
            "legacy database is missing required tables: " + ", ".join(missing)
        )
    sessions = _sessions(connection)
    truth_sets, truth_annotations = _truth_data(connection, tables)
    timeline_references = _timeline_references(
        connection, truth_sets, truth_annotations, tables
    )
    identity_intervals = _identity_intervals(connection, tables)
    voice_samples = _voice_samples(connection, tables)
    effective_windows, window_stats = _effective_identity_windows(
        identity_intervals
    )
    seam_candidates = _seam_candidates(
        connection,
        sessions,
        truth_sets,
        timeline_references,
        tables,
    )
    assessment = _assessment(
        truth_sets,
        timeline_references,
        identity_intervals,
        effective_windows,
        window_stats,
        seam_candidates,
        voice_samples,
    )
    counts = {
        "truth_sets": len(truth_sets),
        "truth_annotations": len(truth_annotations),
        "timeline_reference_candidates": len(timeline_references),
        "identity_records": len(identity_intervals),
        "effective_identity_windows": len(effective_windows),
        "voice_samples": len(voice_samples),
        "seam_candidates": len(seam_candidates),
    }
    schema_version = int(
        connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()[0]
    )
    involved_session_ids = {
        int(value["session_id"])
        for value in (*truth_sets, *identity_intervals)
        if value.get("session_id") is not None
    }
    return {
        "format": LEGACY_LABEL_MIGRATION_FORMAT,
        "policy_version": LEGACY_LABEL_MIGRATION_POLICY,
        "source": {
            "database_sha256": source_sha256,
            "schema_version": schema_version,
            "read_only": True,
        },
        "policy": {
            "identity_window_ms": {
                "minimum": MIN_IDENTITY_WINDOW_MS,
                "maximum": MAX_IDENTITY_WINDOW_MS,
            },
            "identity_holdout_minimums": {
                "self": MIN_SELF_WINDOWS,
                "not_self": MIN_NOT_SELF_WINDOWS,
            },
            "chunk_interval_ms": CHUNK_INTERVAL_MS,
            "seam_window_ms": SEAM_WINDOW_MS,
            "seam_minimums": {
                "reviewed_seams": MIN_REVIEWED_SEAMS,
                "reference_utterances": MIN_REFERENCE_UTTERANCES,
                "overlap_utterances": MIN_OVERLAP_UTTERANCES,
            },
        },
        "sessions": [
            sessions[session_id]
            for session_id in sorted(involved_session_ids)
            if session_id in sessions
        ],
        "truth_sets": truth_sets,
        "truth_annotations": truth_annotations,
        "timeline_reference_candidates": timeline_references,
        "identity_records": identity_intervals,
        "effective_identity_windows": effective_windows,
        "voice_samples": voice_samples,
        "seam_candidates": seam_candidates,
        "counts": counts,
        "assessment": assessment,
    }


def _sessions(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, session_key, recorded_at, timezone, duration_ms, status
        FROM recording_sessions ORDER BY id
        """
    )
    return {
        int(row["id"]): {
            "session_id": int(row["id"]),
            "session_key": str(row["session_key"]),
            "recorded_at": str(row["recorded_at"]),
            "timezone": str(row["timezone"]),
            "duration_ms": int(row["duration_ms"]),
            "status": str(row["status"]),
        }
        for row in rows
    }


def _truth_data(
    connection: sqlite3.Connection, tables: set[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not {"truth_sets", "truth_annotations"}.issubset(tables):
        return [], []
    annotations_by_set: dict[int, Counter[str]] = defaultdict(Counter)
    annotation_rows: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM truth_annotations ORDER BY truth_set_id, id"
    ):
        truth_set_id = int(row["truth_set_id"])
        kind = str(row["annotation_kind"])
        annotations_by_set[truth_set_id][kind] += 1
        annotation_rows.append(
            {
                "legacy_id": int(row["id"]),
                "truth_set_id": truth_set_id,
                "annotation_key": str(row["annotation_key"]),
                "kind": kind,
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
                "label": row["label"],
                "text": row["text"],
                "source_refs": _truth_source_refs(
                    connection, int(row["id"]), tables
                ),
            }
        )
    truth_sets: list[dict[str, Any]] = []
    for row in connection.execute("SELECT * FROM truth_sets ORDER BY id"):
        truth_set_id = int(row["id"])
        completeness = _json_object(row["completeness_json"])
        truth_sets.append(
            {
                "legacy_id": truth_set_id,
                "session_id": int(row["session_id"]),
                "name": str(row["name"]),
                "status": str(row["status"]),
                "scope_start_ms": int(row["scope_start_ms"]),
                "scope_end_ms": int(row["scope_end_ms"]),
                "input_fingerprint": str(row["input_fingerprint"]),
                "truth_sha256": str(row["truth_sha256"]),
                "completeness": {
                    key: _normalize_completeness(completeness.get(key))
                    for key in (
                        "alignment",
                        "transcript",
                        "speaker",
                        "overlap",
                        "identity",
                    )
                },
                "annotation_counts": dict(
                    sorted(annotations_by_set[truth_set_id].items())
                ),
            }
        )
    return truth_sets, annotation_rows


def _timeline_references(
    connection: sqlite3.Connection,
    truth_sets: Sequence[dict[str, Any]],
    annotations: Sequence[dict[str, Any]],
    tables: set[str],
) -> list[dict[str, Any]]:
    sets = {int(value["legacy_id"]): value for value in truth_sets}
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for annotation in annotations:
        grouped[int(annotation["truth_set_id"])].append(annotation)
    output: list[dict[str, Any]] = []
    for transcript in annotations:
        if transcript["kind"] != "transcript" or not transcript["text"]:
            continue
        truth_set_id = int(transcript["truth_set_id"])
        truth_set = sets[truth_set_id]
        related = grouped[truth_set_id]
        speakers = sorted(
            {
                str(value["label"])
                for value in related
                if value["kind"] == "speaker"
                and value["label"]
                and _overlaps(transcript, value)
            }
        )
        identities = sorted(
            {
                _normalize_identity(str(value["label"]))[0]
                for value in related
                if value["kind"] == "identity"
                and value["label"]
                and _overlaps(transcript, value)
            }
        )
        explicit_overlap = any(
            value["kind"] == "overlap" and _overlaps(transcript, value)
            for value in related
        )
        observed_speaker_overlap = _has_speaker_overlap(
            transcript,
            [value for value in related if value["kind"] == "speaker"],
        )
        completeness = truth_set["completeness"]
        has_overlap: bool | None
        if explicit_overlap or observed_speaker_overlap:
            has_overlap = True
        elif (
            completeness["speaker"] == "exhaustive"
            and completeness["overlap"] == "exhaustive"
        ):
            has_overlap = False
        else:
            has_overlap = None
        ready = (
            all(
                completeness[key] == "exhaustive"
                for key in ("alignment", "transcript", "speaker", "overlap")
            )
            and len(speakers) == 1
            and has_overlap is not None
        )
        output.append(
            {
                "candidate_id": f"truth:{truth_set_id}:{transcript['legacy_id']}",
                "session_id": int(truth_set["session_id"]),
                "truth_set_id": truth_set_id,
                "annotation_key": transcript["annotation_key"],
                "start_ms": int(transcript["start_ms"]),
                "end_ms": int(transcript["end_ms"]),
                "text": str(transcript["text"]),
                "speaker_ids": speakers,
                "identity_labels": identities,
                "has_overlap": has_overlap,
                "ready_for_seam_audit": ready,
                "source_refs": _truth_source_refs(
                    connection, int(transcript["legacy_id"]), tables
                ),
            }
        )
    return sorted(
        output,
        key=lambda value: (
            int(value["session_id"]),
            int(value["start_ms"]),
            str(value["candidate_id"]),
        ),
    )


def _identity_intervals(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if {"truth_sets", "truth_annotations"}.issubset(tables):
        rows = connection.execute(
            """
            SELECT ta.*, ts.session_id, ts.status AS truth_status
            FROM truth_annotations ta
            JOIN truth_sets ts ON ts.id = ta.truth_set_id
            WHERE ta.annotation_kind = 'identity'
            ORDER BY ta.id
            """
        )
        for row in rows:
            output.append(
                _identity_record(
                    source_kind="frozen_truth",
                    source_id=str(row["id"]),
                    session_id=int(row["session_id"]),
                    start_ms=int(row["session_start_ms"]),
                    end_ms=int(row["session_end_ms"]),
                    label=str(row["label"] or "unknown"),
                    accepted=str(row["truth_status"]) == "frozen",
                    source_refs=_truth_source_refs(
                        connection, int(row["id"]), tables
                    ),
                    priority=400,
                )
            )
    if "manual_identity_annotations" in tables:
        for row in connection.execute(
            "SELECT * FROM manual_identity_annotations ORDER BY id"
        ):
            session_id = int(row["session_id"])
            start_ms = int(row["session_start_ms"])
            end_ms = int(row["session_end_ms"])
            output.append(
                _identity_record(
                    source_kind="manual_identity",
                    source_id=str(row["id"]),
                    session_id=session_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    label=str(row["identity_label"]),
                    accepted=str(row["status"]) == "active",
                    source_refs=_session_source_refs(
                        connection, session_id, start_ms, end_ms, tables
                    ),
                    priority=500,
                    metadata={
                        "status": str(row["status"]),
                        "anonymous_speaker_label": str(
                            row["anonymous_speaker_label"]
                        ),
                    },
                )
            )
    if "identity_reference_intervals" in tables:
        for row in connection.execute(
            "SELECT * FROM identity_reference_intervals ORDER BY id"
        ):
            accepted = str(row["decision"]) == "confirmed_target"
            source_refs = [
                {
                    "source_object_id": int(row["source_object_id"]),
                    "source_instance_id": int(row["source_instance_id"]),
                    "source_sha256": str(row["source_sha256"]),
                    "source_start_ms": int(row["source_start_ms"]),
                    "source_end_ms": int(row["source_end_ms"]),
                }
            ]
            output.append(
                _identity_record(
                    source_kind="identity_reference",
                    source_id=str(row["id"]),
                    session_id=int(row["session_id"]),
                    start_ms=int(row["session_start_ms"]),
                    end_ms=int(row["session_end_ms"]),
                    label=str(row["identity_label"]),
                    accepted=accepted,
                    source_refs=source_refs,
                    priority=300,
                    metadata={
                        "decision": str(row["decision"]),
                        "provenance_kind": str(row["provenance_kind"]),
                        "provenance_id": int(row["provenance_id"]),
                    },
                )
            )
    if {"segment_identity_annotations", "speech_segments"}.issubset(tables):
        rows = connection.execute(
            """
            SELECT sia.*, ss.start_ms, ss.end_ms, rs.id AS session_id
            FROM segment_identity_annotations sia
            JOIN speech_segments ss ON ss.id = sia.segment_id
            LEFT JOIN recording_sessions rs
              ON rs.legacy_recording_id = sia.recording_id
            ORDER BY sia.id
            """
        )
        for row in rows:
            if row["session_id"] is None:
                continue
            session_id = int(row["session_id"])
            start_ms = int(row["start_ms"])
            end_ms = int(row["end_ms"])
            output.append(
                _identity_record(
                    source_kind="segment_identity",
                    source_id=str(row["id"]),
                    session_id=session_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    label=str(row["identity_label"]),
                    accepted=str(row["annotation_source"]) == "human",
                    source_refs=_session_source_refs(
                        connection, session_id, start_ms, end_ms, tables
                    ),
                    priority=200,
                    metadata={
                        "annotation_source": str(row["annotation_source"]),
                        "confidence": str(row["confidence"]),
                    },
                )
            )
    if {
        "v2d1_identity_labels",
        "v2d1_candidate_reviews",
        "processing_runs",
    }.issubset(tables):
        rows = connection.execute(
            """
            SELECT labels.id, labels.identity_label, review.session_start_ms,
                   review.session_end_ms, review.status, runs.session_id
            FROM v2d1_identity_labels labels
            JOIN v2d1_candidate_reviews review
              ON review.run_id = labels.run_id
             AND review.candidate_id = labels.candidate_id
            JOIN processing_runs runs ON runs.id = labels.run_id
            ORDER BY labels.id
            """
        )
        for row in rows:
            session_id = int(row["session_id"])
            start_ms = int(row["session_start_ms"])
            end_ms = int(row["session_end_ms"])
            output.append(
                _identity_record(
                    source_kind="v2d1_review",
                    source_id=str(row["id"]),
                    session_id=session_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    label=str(row["identity_label"]),
                    accepted=str(row["status"]) == "confirmed_speech",
                    source_refs=_session_source_refs(
                        connection, session_id, start_ms, end_ms, tables
                    ),
                    priority=350,
                    metadata={"review_status": str(row["status"])},
                )
            )
    return sorted(
        output,
        key=lambda value: (
            int(value["session_id"]),
            int(value["start_ms"]),
            -int(value["source_priority"]),
            str(value["record_id"]),
        ),
    )


def _identity_record(
    *,
    source_kind: str,
    source_id: str,
    session_id: int,
    start_ms: int,
    end_ms: int,
    label: str,
    accepted: bool,
    source_refs: list[dict[str, Any]],
    priority: int,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    identity, media_playback = _normalize_identity(label)
    reasons: list[str] = []
    if not accepted:
        reasons.append("not_active_or_confirmed")
    if identity == "unknown":
        reasons.append("identity_is_unknown")
    if media_playback:
        reasons.append("media_playback_is_separate_robustness_evidence")
    if end_ms - start_ms < MIN_IDENTITY_WINDOW_MS:
        reasons.append("shorter_than_minimum_window")
    if not source_refs:
        reasons.append("missing_source_coordinates")
    return {
        "record_id": f"legacy:{source_kind}:{source_id}",
        "source_kind": source_kind,
        "source_priority": priority,
        "session_id": session_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "subject_label": label,
        "identity": identity,
        "media_playback": media_playback,
        "accepted_human_label": accepted,
        "eligible_for_calibration_window": not reasons,
        "ineligible_reasons": reasons,
        "source_refs": source_refs,
        "metadata": dict(metadata or {}),
    }


def _effective_identity_windows(
    records: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[tuple[int, str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        if not record["eligible_for_calibration_window"]:
            continue
        grouped[
            (
                int(record["session_id"]),
                str(record["identity"]),
                str(record["subject_label"]).casefold(),
            )
        ].append(record)
    candidates: list[dict[str, Any]] = []
    for (session_id, identity, subject), values in sorted(grouped.items()):
        merged = _merge_ranges(values)
        for start_ms, end_ms, provenance in merged:
            cursor = start_ms
            while end_ms - cursor >= MIN_IDENTITY_WINDOW_MS:
                window_end = min(end_ms, cursor + MAX_IDENTITY_WINDOW_MS)
                candidates.append(
                    {
                        "window_id": (
                            f"identity-window:{session_id}:{cursor}:{window_end}:"
                            f"{identity}:{subject}"
                        ),
                        "session_id": session_id,
                        "start_ms": cursor,
                        "end_ms": window_end,
                        "identity": identity,
                        "subject_label": subject,
                        "provenance_record_ids": sorted(provenance),
                    }
                )
                cursor = window_end
    selected: list[dict[str, Any]] = []
    rejected_ids: set[str] = set()
    duplicate_windows = 0
    conflict_windows = 0
    for candidate in sorted(
        candidates,
        key=lambda value: (
            int(value["session_id"]),
            int(value["start_ms"]),
            str(value["identity"]),
            str(value["subject_label"]),
        ),
    ):
        overlaps = [
            value
            for value in selected
            if value["session_id"] == candidate["session_id"]
            and _overlaps(candidate, value)
        ]
        if not overlaps:
            selected.append(candidate)
            continue
        if all(
            value["identity"] == candidate["identity"]
            and value["subject_label"] == candidate["subject_label"]
            for value in overlaps
        ):
            duplicate_windows += 1
            continue
        conflict_windows += 1
        rejected_ids.add(str(candidate["window_id"]))
        rejected_ids.update(str(value["window_id"]) for value in overlaps)
    selected = [
        value
        for value in selected
        if str(value["window_id"]) not in rejected_ids
    ]
    return selected, {
        "duplicate_windows_removed": duplicate_windows,
        "conflicting_windows_removed": conflict_windows,
    }


def _merge_ranges(
    records: Sequence[dict[str, Any]],
) -> list[tuple[int, int, set[str]]]:
    merged: list[tuple[int, int, set[str]]] = []
    for record in sorted(
        records,
        key=lambda value: (
            int(value["start_ms"]),
            int(value["end_ms"]),
            -int(value["source_priority"]),
        ),
    ):
        start_ms = int(record["start_ms"])
        end_ms = int(record["end_ms"])
        provenance = {str(record["record_id"])}
        if merged and start_ms <= merged[-1][1]:
            previous_start, previous_end, previous_provenance = merged[-1]
            merged[-1] = (
                previous_start,
                max(previous_end, end_ms),
                previous_provenance | provenance,
            )
        else:
            merged.append((start_ms, end_ms, provenance))
    return merged


def _voice_samples(
    connection: sqlite3.Connection, tables: set[str]
) -> list[dict[str, Any]]:
    if "voice_library_samples" not in tables:
        return []
    output: list[dict[str, Any]] = []
    for row in connection.execute(
        "SELECT * FROM voice_library_samples ORDER BY id"
    ):
        identity, media_playback = _normalize_identity(
            str(row["identity_label"])
        )
        split = str(row["split"])
        speech_ms = int(row["speech_ms"])
        has_embedding = row["embedding_blob"] is not None
        role = "enrollment" if split == "accepted" else "holdout"
        reasons: list[str] = []
        if role == "enrollment":
            reasons.append("enrollment_cannot_be_its_own_holdout")
        if identity == "unknown":
            reasons.append("identity_is_unknown")
        if media_playback:
            reasons.append("media_playback_is_a_separate_robustness_set")
        if speech_ms < MIN_IDENTITY_WINDOW_MS:
            reasons.append("shorter_than_minimum_window")
        if not has_embedding:
            reasons.append("embedding_not_generated")
        output.append(
            {
                "legacy_id": int(row["id"]),
                "sample_key": str(row["sample_key"]),
                "session_key": str(row["session_key"]),
                "split": split,
                "role": role,
                "subject_label": str(row["identity_label"]),
                "identity": identity,
                "media_playback": media_playback,
                "duration_ms": int(row["duration_ms"]),
                "speech_ms": speech_ms,
                "has_embedding": has_embedding,
                "embedding_model": row["embedding_model"],
                "embedding_version": row["embedding_version"],
                "source_path": row["source_path"],
                "stored_path": row["stored_path"],
                "source_sha256": row["source_sha256"],
                "recording_id": row["recording_id"],
                "segment_id": row["segment_id"],
                "eligible_scored_holdout": not reasons,
                "ineligible_reasons": reasons,
            }
        )
    return output


def _seam_candidates(
    connection: sqlite3.Connection,
    sessions: Mapping[int, dict[str, Any]],
    truth_sets: Sequence[dict[str, Any]],
    references: Sequence[dict[str, Any]],
    tables: set[str],
) -> list[dict[str, Any]]:
    truth_session_ids = {int(value["session_id"]) for value in truth_sets}
    predictions = _latest_predictions(connection, tables)
    output: list[dict[str, Any]] = []
    for session_id in sorted(truth_session_ids):
        session = sessions.get(session_id)
        if session is None:
            continue
        offset_ms = CHUNK_INTERVAL_MS
        while offset_ms < int(session["duration_ms"]):
            scoped_references = [
                value
                for value in references
                if int(value["session_id"]) == session_id
                and _near_seam(value, offset_ms)
            ]
            scoped_predictions = [
                value
                for value in predictions.get(session_id, [])
                if _near_seam(value, offset_ms)
            ]
            prediction_counts = Counter(
                str(value["kind"]) for value in scoped_predictions
            )
            output.append(
                {
                    "seam_id": f"legacy-session:{session_id}:seam:{offset_ms}",
                    "session_id": session_id,
                    "offset_ms": offset_ms,
                    "left_chunk_id": (
                        f"legacy-session:{session_id}:chunk:"
                        f"{offset_ms // CHUNK_INTERVAL_MS - 1}"
                    ),
                    "right_chunk_id": (
                        f"legacy-session:{session_id}:chunk:"
                        f"{offset_ms // CHUNK_INTERVAL_MS}"
                    ),
                    "legacy_reference_candidates": len(scoped_references),
                    "ready_reference_candidates": sum(
                        bool(value["ready_for_seam_audit"])
                        for value in scoped_references
                    ),
                    "confirmed_overlap_candidates": sum(
                        value["has_overlap"] is True
                        for value in scoped_references
                    ),
                    "prediction_hints": dict(sorted(prediction_counts.items())),
                    "human_reviewed_for_v31": False,
                }
            )
            offset_ms += CHUNK_INTERVAL_MS
    return output


def _latest_predictions(
    connection: sqlite3.Connection, tables: set[str]
) -> dict[int, list[dict[str, Any]]]:
    required = {"benchmark_prediction_sets", "benchmark_predictions"}
    if not required.issubset(tables):
        return {}
    latest: dict[tuple[int, str], int] = {}
    rows = connection.execute(
        """
        SELECT bps.session_id, bp.prediction_kind, MAX(bps.id) AS set_id
        FROM benchmark_prediction_sets bps
        JOIN benchmark_predictions bp ON bp.prediction_set_id = bps.id
        WHERE bp.prediction_kind IN ('transcript', 'speaker', 'overlap')
        GROUP BY bps.session_id, bp.prediction_kind
        """
    )
    for row in rows:
        latest[(int(row["session_id"]), str(row["prediction_kind"]))] = int(
            row["set_id"]
        )
    output: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for (session_id, kind), set_id in sorted(latest.items()):
        prediction_rows = connection.execute(
            """
            SELECT session_start_ms, session_end_ms
            FROM benchmark_predictions
            WHERE prediction_set_id = ? AND prediction_kind = ?
            ORDER BY session_start_ms, id
            """,
            (set_id, kind),
        )
        output[session_id].extend(
            {
                "kind": kind,
                "start_ms": int(row["session_start_ms"]),
                "end_ms": int(row["session_end_ms"]),
            }
            for row in prediction_rows
        )
    return dict(output)


def _assessment(
    truth_sets: Sequence[dict[str, Any]],
    references: Sequence[dict[str, Any]],
    identity_records: Sequence[dict[str, Any]],
    effective_windows: Sequence[dict[str, Any]],
    window_stats: Mapping[str, int],
    seams: Sequence[dict[str, Any]],
    voice_samples: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    self_windows = [
        value for value in effective_windows if value["identity"] == "self"
    ]
    not_self_windows = [
        value
        for value in effective_windows
        if value["identity"] == "not_self"
    ]
    prefilled_seams = [
        value for value in seams if value["legacy_reference_candidates"] > 0
    ]
    ready_seams = [
        value for value in seams if value["ready_reference_candidates"] > 0
    ]
    ranked_seams = sorted(
        seams,
        key=lambda value: (
            -int(value["legacy_reference_candidates"] > 0),
            -int(value["prediction_hints"].get("overlap", 0)),
            -int(value["prediction_hints"].get("transcript", 0)),
            -int(value["prediction_hints"].get("speaker", 0)),
            int(value["session_id"]),
            int(value["offset_ms"]),
        ),
    )
    recommended = [str(value["seam_id"]) for value in ranked_seams[:10]]
    scored_self_holdout = sum(
        bool(value["eligible_scored_holdout"])
        and value["identity"] == "self"
        for value in voice_samples
    )
    scored_not_self_holdout = sum(
        bool(value["eligible_scored_holdout"])
        and value["identity"] == "not_self"
        for value in voice_samples
    )
    identity_assessment = {
        "migrated_records": len(identity_records),
        "effective_self_windows": len(self_windows),
        "effective_not_self_windows": len(not_self_windows),
        "self_sessions": len({value["session_id"] for value in self_windows}),
        "not_self_sessions": len(
            {value["session_id"] for value in not_self_windows}
        ),
        "media_records": sum(
            bool(value["media_playback"]) for value in identity_records
        ),
        "already_scored_eligible_self_holdout_samples": scored_self_holdout,
        "already_scored_eligible_not_self_holdout_samples": (
            scored_not_self_holdout
        ),
        "additional_self_labels_needed": max(
            0, MIN_SELF_WINDOWS - len(self_windows)
        ),
        "additional_not_self_labels_needed": max(
            0, MIN_NOT_SELF_WINDOWS - len(not_self_windows)
        ),
        "automatic_embedding_and_scoring_required": True,
        **dict(window_stats),
    }
    seam_reference_count = sum(
        int(value["legacy_reference_candidates"]) for value in seams
    )
    confirmed_overlap_count = sum(
        int(value["confirmed_overlap_candidates"]) for value in seams
    )
    seam_assessment = {
        "migrated_truth_sets": len(truth_sets),
        "migrated_reference_candidates": len(references),
        "seams_with_legacy_prefill": len(prefilled_seams),
        "fully_ready_seams": len(ready_seams),
        "reference_candidates_near_seams": seam_reference_count,
        "confirmed_overlap_candidates_near_seams": confirmed_overlap_count,
        "seams_requiring_final_review": MIN_REVIEWED_SEAMS,
        "additional_unprefilled_seams_needed": max(
            0, MIN_REVIEWED_SEAMS - len(prefilled_seams)
        ),
        "reference_gap_before_review_and_splitting": max(
            0, MIN_REFERENCE_UTTERANCES - seam_reference_count
        ),
        "confirmed_overlap_gap": max(
            0, MIN_OVERLAP_UTTERANCES - confirmed_overlap_count
        ),
        "recommended_seam_ids": recommended,
    }
    return {
        "identity": identity_assessment,
        "seam_quality": seam_assessment,
        "manual_labeling": {
            "identity_needed": bool(
                identity_assessment["additional_self_labels_needed"]
                or identity_assessment["additional_not_self_labels_needed"]
            ),
            "seam_review_needed": True,
        },
    }


def _render_assessment(
    assessment: Mapping[str, Any], counts: Mapping[str, int]
) -> str:
    identity = assessment["identity"]
    seam = assessment["seam_quality"]
    self_gap = int(identity["additional_self_labels_needed"])
    not_self_gap = int(identity["additional_not_self_labels_needed"])
    identity_result = (
        "现有人工身份标注数量已达到窗口门槛；不需要先补标，下一步是自动抽取、"
        "生成 embedding 并跑独立校准。"
        if self_gap == 0 and not_self_gap == 0
        else (
            f"去重后还缺本人 {self_gap} 个、真人非本人 {not_self_gap} 个有效窗口。"
        )
    )
    return "\n".join(
        [
            "# V3.1 旧标注迁移与补标评估",
            "",
            "## 迁移结果",
            "",
            f"- 冻结真值集：{counts['truth_sets']} 套。",
            f"- 原始真值标注：{counts['truth_annotations']} 条。",
            f"- 时间线文字候选：{counts['timeline_reference_candidates']} 条。",
            f"- 身份来源记录：{counts['identity_records']} 条。",
            f"- 去重后的有效身份窗口：{counts['effective_identity_windows']} 个。",
            "",
            "旧数据库只读，迁移包只追加发布；预测提示没有被当成人工真值。",
            "",
            "## 本人识别",
            "",
            f"- 有效本人窗口：{identity['effective_self_windows']} / {MIN_SELF_WINDOWS}。",
            (
                "- 有效真人非本人窗口："
                f"{identity['effective_not_self_windows']} / {MIN_NOT_SELF_WINDOWS}。"
            ),
            f"- 单独保留的电视/媒体记录：{identity['media_records']} 条。",
            f"- 结论：{identity_result}",
            "",
            "## 接缝质量",
            "",
            f"- 已有文字可预填的接缝：{seam['seams_with_legacy_prefill']} 个。",
            f"- 已完全满足 V3.1 真值要求的接缝：{seam['fully_ready_seams']} 个。",
            (
                "- 仍需新选且从头检查的接缝：至少 "
                f"{seam['additional_unprefilled_seams_needed']} 个。"
            ),
            (
                "- 仍需人工确认的重叠讲话：至少 "
                f"{seam['confirmed_overlap_gap']} 条。"
            ),
            "- 所有最终入选接缝仍需补齐精确边界、文字、说话人与重叠标记。",
            "",
            "## 下一步",
            "",
            "先自动生成身份 embedding 和校准结果；身份数量真的不足时再补标。",
            "接缝按迁移包中的 recommended_seam_ids 建立试听队列，完成后运行 seam-audit。",
            "",
        ]
    )


def _truth_source_refs(
    connection: sqlite3.Connection, annotation_id: int, tables: set[str]
) -> list[dict[str, Any]]:
    if "truth_annotation_sources" not in tables:
        return []
    return [
        {
            "source_object_id": int(row["source_object_id"]),
            "source_instance_id": (
                int(row["source_instance_id"])
                if row["source_instance_id"] is not None
                else None
            ),
            "source_sha256": str(row["source_sha256"]),
            "source_start_ms": int(row["source_start_ms"]),
            "source_end_ms": int(row["source_end_ms"]),
        }
        for row in connection.execute(
            """
            SELECT * FROM truth_annotation_sources
            WHERE annotation_id = ? ORDER BY position
            """,
            (annotation_id,),
        )
    ]


def _session_source_refs(
    connection: sqlite3.Connection,
    session_id: int,
    start_ms: int,
    end_ms: int,
    tables: set[str],
) -> list[dict[str, Any]]:
    if not {"session_sources", "source_objects"}.issubset(tables):
        return []
    rows = connection.execute(
        """
        SELECT ss.*, so.sha256
        FROM session_sources ss
        JOIN source_objects so ON so.id = ss.source_object_id
        WHERE ss.session_id = ?
          AND ss.session_start_ms < ?
          AND ss.session_end_ms > ?
        ORDER BY ss.chunk_index
        """,
        (session_id, end_ms, start_ms),
    )
    output: list[dict[str, Any]] = []
    for row in rows:
        overlap_start = max(start_ms, int(row["session_start_ms"]))
        overlap_end = min(end_ms, int(row["session_end_ms"]))
        source_start = int(row["source_start_ms"]) + (
            overlap_start - int(row["session_start_ms"])
        )
        source_end = source_start + (overlap_end - overlap_start)
        output.append(
            {
                "source_object_id": int(row["source_object_id"]),
                "source_instance_id": int(row["source_instance_id"]),
                "source_sha256": str(row["sha256"]),
                "source_start_ms": source_start,
                "source_end_ms": source_end,
            }
        )
    return output


def _normalize_identity(value: str) -> tuple[str, bool]:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"self", "我", "本人"}:
        return "self", False
    if normalized in {
        "not_self",
        "mother",
        "mom",
        "母亲",
        "妈妈",
        "father",
        "dad",
        "父亲",
        "爸爸",
    }:
        return "not_self", False
    if normalized in {"tv", "television", "media", "电视"}:
        return "not_self", True
    return "unknown", False


def _normalize_completeness(value: Any) -> str:
    normalized = str(value or "none").strip().casefold()
    if normalized == "exhaustive":
        return "exhaustive"
    if normalized == "none":
        return "none"
    return "sparse"


def _has_speaker_overlap(
    transcript: Mapping[str, Any], speakers: Sequence[Mapping[str, Any]]
) -> bool:
    scoped = [value for value in speakers if _overlaps(transcript, value)]
    for index, left in enumerate(scoped):
        for right in scoped[index + 1 :]:
            if left.get("label") != right.get("label") and _overlaps(left, right):
                return True
    return False


def _near_seam(value: Mapping[str, Any], offset_ms: int) -> bool:
    return (
        int(value["start_ms"]) < offset_ms + SEAM_WINDOW_MS
        and int(value["end_ms"]) > offset_ms - SEAM_WINDOW_MS
    )


def _overlaps(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        int(left["start_ms"]) < int(right["end_ms"])
        and int(left["end_ms"]) > int(right["start_ms"])
    )


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _database_fingerprint(path: Path) -> str:
    main_sha256 = _file_sha256(path)
    wal_path = Path(f"{path}-wal")
    if not wal_path.is_file() or wal_path.stat().st_size == 0:
        return main_sha256
    digest = hashlib.sha256()
    digest.update(b"sqlite-main\0")
    digest.update(main_sha256.encode("ascii"))
    digest.update(b"\0sqlite-wal\0")
    digest.update(_file_sha256(wal_path).encode("ascii"))
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_json(path: Path, value: Mapping[str, Any]) -> None:
    _publish_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _publish_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = value.encode("utf-8")
    if path.exists():
        if path.read_bytes() == encoded:
            return
        raise FileExistsError(f"refusing to overwrite migration output: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "LEGACY_LABEL_MIGRATION_FORMAT",
    "LEGACY_LABEL_MIGRATION_POLICY",
    "LegacyLabelMigrationSummary",
    "migrate_legacy_labels",
]
