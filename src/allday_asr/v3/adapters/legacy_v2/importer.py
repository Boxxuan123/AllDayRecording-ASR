from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from allday_asr.v3.application.legacy_import import (
    LegacyImportCommand,
    LegacyImportResult,
)
from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.models import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    CorrectionOperation,
    Device,
    DeviceKind,
    DeviceStatus,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.stores import ContentStore


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]

_REQUIRED_TABLES = {
    "schema_migrations",
    "recording_sessions",
    "source_objects",
    "source_instances",
    "session_sources",
    "processing_runs",
}
_DIRECT_TABLES = _REQUIRED_TABLES | {
    "derived_artifacts",
    "session_manifests",
}
_CORRECTION_TABLES = {
    "segment_identity_annotations",
    "identity_candidate_reviews",
    "v2d1_candidate_reviews",
    "manual_identity_annotations",
    "semantic_candidate_revisions",
}
_SNAPSHOT_TABLES = {
    "recordings",
    "processing_stages",
    "person_profiles",
    "speech_segments",
    "voice_library_samples",
    "conversation_events",
    "evaluation_runs",
    "action_candidates",
    "source_integrity_audits",
    "processing_run_inputs",
    "truth_sets",
    "truth_annotations",
    "truth_annotation_sources",
    "benchmark_prediction_sets",
    "benchmark_predictions",
    "benchmark_runs",
    "asr_hypotheses",
    "asr_alignment_tokens",
    "asr_token_sources",
    "asr_disagreements",
    "diarization_turns",
    "diarization_turn_sources",
    "token_speaker_attributions",
    "semantic_exchanges",
    "semantic_candidates",
    "v2d1_review_completions",
    "v2d1_identity_labels",
    "identity_reference_intervals",
    "session_backups",
    "session_backup_files",
} | _CORRECTION_TABLES
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class _PreparedImport:
    device: Device
    sessions: list[RecordingSession] = field(default_factory=list)
    assets: list[AudioAsset] = field(default_factory=list)
    replicas: list[AudioReplica] = field(default_factory=list)
    segments: list[CaptureSegment] = field(default_factory=list)
    manifests: list[SessionManifest] = field(default_factory=list)
    processing_runs: list[ProcessingRun] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    corrections: list[CorrectionOperation] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    unmapped: dict[str, int] = field(default_factory=dict)


class LegacyV2Importer:
    """Read a V2 database through a query-only connection and import into V3."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        audio_store: ContentStore,
        artifact_store: ContentStore,
        *,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._audio_store = audio_store
        self._artifact_store = artifact_store
        self._now = now or (lambda: datetime.now(timezone.utc))

    def import_database(self, command: LegacyImportCommand) -> LegacyImportResult:
        source = command.source_database.resolve(strict=True)
        source_sha256 = _database_fingerprint(source)
        import_id = new_ulid()
        connection = _open_read_only(source)
        try:
            tables = _table_names(connection)
            missing = _REQUIRED_TABLES - tables
            if missing:
                raise ValueError(
                    "legacy V2 database is missing required tables: "
                    + ", ".join(sorted(missing))
                )
            schema_version = _schema_version(connection)
            namespace = command.source_namespace or source_sha256[:24]
            prepared = self._prepare(connection, source, namespace, tables)
        finally:
            connection.close()
        if _database_fingerprint(source) != source_sha256:
            raise RuntimeError("legacy V2 database changed while it was imported")
        return self._persist(
            import_id=import_id,
            source=source,
            source_sha256=source_sha256,
            source_schema_version=schema_version,
            source_namespace=namespace,
            prepared=prepared,
        )

    def _prepare(
        self,
        connection: sqlite3.Connection,
        source_database: Path,
        namespace: str,
        tables: set[str],
    ) -> _PreparedImport:
        now = self._now()
        device_ref = _legacy_ref(namespace, "device", "computer")
        prepared = _PreparedImport(
            device=Device(
                device_id=stable_ulid(device_ref),
                kind=DeviceKind.COMPUTER,
                name="Legacy V2 Computer",
                status=DeviceStatus.ACTIVE,
                revision=1,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
                legacy_ref=device_ref,
            )
        )
        session_rows = {
            int(row["id"]): row
            for row in connection.execute(
                "SELECT * FROM recording_sessions ORDER BY id"
            )
        }
        legacy_recording_to_session = {
            int(row["legacy_recording_id"]): session_id
            for session_id, row in session_rows.items()
            if row["legacy_recording_id"] is not None
        }
        bad_session_ids: set[int] = set()
        asset_by_source_object: dict[int, AudioAsset] = {}
        asset_by_sha: dict[str, AudioAsset] = {}
        seen_paths: dict[str, str] = {}

        for row in _source_rows(connection):
            session_id = int(row["session_id"])
            source_object_id = int(row["source_object_id"])
            source_instance_id = int(row["source_instance_id"])
            instance_ref = _legacy_ref(namespace, "source_instances", source_instance_id)
            if session_id not in session_rows:
                _issue(
                    prepared,
                    "unmapped_reference",
                    instance_ref,
                    "session source references a missing recording session",
                )
                _unmapped(prepared, "session_sources")
                continue
            try:
                sha256 = _sha256(str(row["sha256"]))
                size_bytes = int(row["byte_size"])
                duration_ms = int(row["duration_ms"])
                audio_format = _audio_format(
                    row["container"], str(row["source_path"])
                )
            except (TypeError, ValueError) as exc:
                bad_session_ids.add(session_id)
                _issue(prepared, "invalid_source_metadata", instance_ref, str(exc))
                _unmapped(prepared, "source_instances")
                continue

            asset_ref = _legacy_ref(namespace, "source_objects", source_object_id)
            asset = asset_by_source_object.get(source_object_id)
            if asset is None:
                asset = asset_by_sha.get(sha256)
            if asset is None:
                asset = AudioAsset(
                    asset_id=stable_ulid(asset_ref),
                    sha256=sha256,
                    size_bytes=size_bytes,
                    duration_ms=duration_ms,
                    format=audio_format,
                    media_id=stable_ulid("media", sha256),
                    created_at=_legacy_datetime(row["source_created_at"], "UTC"),
                    legacy_ref=asset_ref,
                )
                prepared.assets.append(asset)
                asset_by_sha[sha256] = asset
            if source_object_id not in asset_by_source_object:
                asset_by_source_object[source_object_id] = asset

            path = _resolve_source_path(str(row["source_path"]), source_database)
            normalized_path = str(path).casefold()
            previous_sha = seen_paths.get(normalized_path)
            if previous_sha is not None and previous_sha != sha256:
                bad_session_ids.add(session_id)
                _issue(
                    prepared,
                    "conflicting_path",
                    instance_ref,
                    "the same legacy path declares different SHA-256 values",
                    path=str(path),
                    first_sha256=previous_sha,
                    second_sha256=sha256,
                )
            else:
                seen_paths[normalized_path] = sha256

            replica_state = AudioReplicaState.QUARANTINED
            verified_at: datetime | None = None
            storage_key = f"legacy-quarantine/{stable_ulid(instance_ref)}"
            if not path.is_file():
                bad_session_ids.add(session_id)
                _issue(
                    prepared,
                    "missing_file",
                    instance_ref,
                    "legacy source file is missing",
                    path=str(path),
                    expected_sha256=sha256,
                )
            else:
                actual_sha256 = _file_sha256(path)
                actual_size = path.stat().st_size
                if actual_sha256 != sha256 or actual_size != size_bytes:
                    bad_session_ids.add(session_id)
                    _issue(
                        prepared,
                        "source_mismatch",
                        instance_ref,
                        "legacy source bytes do not match stored metadata",
                        path=str(path),
                        expected_sha256=sha256,
                        actual_sha256=actual_sha256,
                        expected_size=size_bytes,
                        actual_size=actual_size,
                    )
                else:
                    stored = self._audio_store.put_file(
                        path, expected_sha256=sha256
                    )
                    storage_key = stored.storage_key
                    replica_state = AudioReplicaState.AVAILABLE
                    verified_at = now

            replica = AudioReplica(
                replica_id=stable_ulid(instance_ref),
                asset_id=asset.asset_id,
                device_id=prepared.device.device_id,
                storage_key=storage_key,
                state=replica_state,
                verified_at=verified_at,
                created_at=_legacy_datetime(row["instance_created_at"], "UTC"),
                legacy_ref=instance_ref,
            )
            prepared.replicas.append(replica)
            segment_ref = _legacy_ref(namespace, "session_sources", int(row["mapping_id"]))
            try:
                segment = CaptureSegment(
                    segment_id=stable_ulid(segment_ref),
                    session_id=stable_ulid(
                        _legacy_ref(namespace, "recording_sessions", session_id)
                    ),
                    asset_id=asset.asset_id,
                    replica_id=replica.replica_id,
                    sequence=int(row["chunk_index"]),
                    session_start_ms=int(row["session_start_ms"]),
                    session_end_ms=int(row["session_end_ms"]),
                    source_start_ms=int(row["source_start_ms"]),
                    source_end_ms=int(row["source_end_ms"]),
                    start_sample=(
                        int(row["session_start_sample"])
                        if row["session_start_sample"] is not None
                        else None
                    ),
                    captured_at=_legacy_datetime(
                        row["instance_recorded_at"], str(row["instance_timezone"])
                    ),
                    legacy_ref=segment_ref,
                )
            except (TypeError, ValueError) as exc:
                bad_session_ids.add(session_id)
                _issue(prepared, "invalid_segment", segment_ref, str(exc))
                _unmapped(prepared, "session_sources")
            else:
                prepared.segments.append(segment)

        for legacy_id, row in session_rows.items():
            session_ref = _legacy_ref(namespace, "recording_sessions", legacy_id)
            captured_start = _legacy_datetime(row["recorded_at"], str(row["timezone"]))
            duration_ms = int(row["duration_ms"])
            quarantined = legacy_id in bad_session_ids
            prepared.sessions.append(
                RecordingSession(
                    session_id=stable_ulid(session_ref),
                    captured_start=captured_start,
                    captured_end=captured_start + timedelta(milliseconds=duration_ms),
                    timezone=str(row["timezone"]),
                    state=(
                        RecordingSessionState.QUARANTINED
                        if quarantined
                        else RecordingSessionState.COMPUTER_INGESTED
                    ),
                    revision=1,
                    status_code="failed" if quarantined else "backup_required",
                    current_stage="legacy_import",
                    progress=1.0,
                    blocking_reason=(
                        "legacy_source_issue" if quarantined else "backup_required"
                    ),
                    created_at=_legacy_datetime(row["created_at"], "UTC"),
                    updated_at=_legacy_datetime(row["updated_at"], "UTC"),
                    legacy_ref=session_ref,
                )
            )

        run_by_legacy_id: dict[int, ProcessingRun] = {}
        if "processing_runs" in tables:
            for row in connection.execute("SELECT * FROM processing_runs ORDER BY id"):
                legacy_session_id = _run_session_id(row, legacy_recording_to_session)
                run_ref = _legacy_ref(namespace, "processing_runs", int(row["id"]))
                if legacy_session_id is None or legacy_session_id not in session_rows:
                    _issue(
                        prepared,
                        "unmapped_reference",
                        run_ref,
                        "processing run cannot be linked to a recording session",
                    )
                    _unmapped(prepared, "processing_runs")
                    continue
                run = _processing_run(namespace, row, legacy_session_id)
                run_by_legacy_id[int(row["id"])] = run
                prepared.processing_runs.append(run)
                prepared.artifacts.append(
                    self._row_artifact(
                        namespace,
                        table="processing_runs",
                        key=str(row["id"]),
                        rows=[row],
                        run_id=run.run_id,
                        input_refs=(run.session_id,),
                        created_at=run.created_at,
                        kind="legacy_processing_run",
                    )
                )

        self._prepare_manifests(
            connection,
            source_database,
            namespace,
            session_rows,
            prepared,
        )
        self._prepare_derived_artifacts(
            connection,
            source_database,
            namespace,
            run_by_legacy_id,
            asset_by_source_object,
            prepared,
        )
        self._prepare_corrections(connection, namespace, tables, prepared)
        self._prepare_snapshots(connection, namespace, tables, prepared)
        known = _DIRECT_TABLES | _SNAPSHOT_TABLES | {"sqlite_sequence"}
        for table in sorted(tables - known):
            count = _table_count(connection, table)
            if count:
                prepared.unmapped[table] = count
                _issue(
                    prepared,
                    "unmapped_table",
                    _legacy_ref(namespace, "table", table),
                    "legacy table is not recognized by this importer version",
                    row_count=count,
                )
        return prepared

    def _prepare_manifests(
        self,
        connection: sqlite3.Connection,
        source_database: Path,
        namespace: str,
        sessions: Mapping[int, sqlite3.Row],
        prepared: _PreparedImport,
    ) -> None:
        if not _has_table(connection, "session_manifests"):
            return
        for row in connection.execute("SELECT * FROM session_manifests ORDER BY id"):
            legacy_session_id = int(row["session_id"])
            manifest_ref = _legacy_ref(namespace, "session_manifests", int(row["id"]))
            if legacy_session_id not in sessions:
                _unmapped(prepared, "session_manifests")
                _issue(
                    prepared,
                    "unmapped_reference",
                    manifest_ref,
                    "manifest references a missing session",
                )
                continue
            try:
                sha256 = _sha256(str(row["manifest_sha256"]))
            except ValueError as exc:
                _unmapped(prepared, "session_manifests")
                _issue(
                    prepared,
                    "invalid_manifest_metadata",
                    manifest_ref,
                    str(exc),
                )
                continue
            path = _resolve_source_path(str(row["manifest_path"]), source_database)
            storage_ref = f"legacy-missing:{stable_ulid(manifest_ref)}"
            if path.is_file() and _file_sha256(path) == sha256:
                storage_ref = self._artifact_store.put_file(
                    path, expected_sha256=sha256
                ).storage_key
            else:
                _issue(
                    prepared,
                    "missing_or_mismatched_manifest",
                    manifest_ref,
                    "legacy manifest bytes are unavailable or mismatched",
                    path=str(path),
                )
            prepared.manifests.append(
                SessionManifest(
                    manifest_id=stable_ulid(manifest_ref),
                    session_id=stable_ulid(
                        _legacy_ref(
                            namespace, "recording_sessions", legacy_session_id
                        )
                    ),
                    schema_version=str(row["manifest_format"]),
                    sha256=sha256,
                    storage_ref=storage_ref,
                    entries=_json_object(row["summary_json"]),
                    created_at=_legacy_datetime(row["created_at"], "UTC"),
                    legacy_ref=manifest_ref,
                )
            )

    def _prepare_derived_artifacts(
        self,
        connection: sqlite3.Connection,
        source_database: Path,
        namespace: str,
        runs: Mapping[int, ProcessingRun],
        assets: Mapping[int, AudioAsset],
        prepared: _PreparedImport,
    ) -> None:
        if not _has_table(connection, "derived_artifacts"):
            return
        for row in connection.execute("SELECT * FROM derived_artifacts ORDER BY id"):
            artifact_ref = _legacy_ref(namespace, "derived_artifacts", int(row["id"]))
            path = _resolve_source_path(str(row["artifact_path"]), source_database)
            expected: str | None = None
            if row["sha256"]:
                try:
                    expected = _sha256(str(row["sha256"]))
                except ValueError as exc:
                    _unmapped(prepared, "derived_artifacts")
                    _issue(
                        prepared,
                        "invalid_artifact_metadata",
                        artifact_ref,
                        str(exc),
                    )
            storage_ref = f"legacy-missing:{stable_ulid(artifact_ref)}"
            status = "quarantined"
            digest = expected
            size_bytes = int(row["byte_size"]) if row["byte_size"] is not None else None
            if path.is_file():
                try:
                    stored = self._artifact_store.put_file(
                        path, expected_sha256=expected
                    )
                except ValueError as exc:
                    _issue(prepared, "artifact_mismatch", artifact_ref, str(exc))
                else:
                    storage_ref = stored.storage_key
                    status = "active"
                    digest = stored.sha256
                    size_bytes = stored.size_bytes
            else:
                _issue(
                    prepared,
                    "missing_artifact",
                    artifact_ref,
                    "legacy derived artifact file is missing",
                    path=str(path),
                )
            legacy_run_id = int(row["run_id"]) if row["run_id"] is not None else None
            run = runs.get(legacy_run_id) if legacy_run_id is not None else None
            input_refs: list[str] = []
            source_object_id = row["source_object_id"]
            if source_object_id is not None and int(source_object_id) in assets:
                input_refs.append(assets[int(source_object_id)].asset_id)
            prepared.artifacts.append(
                Artifact(
                    artifact_id=stable_ulid(artifact_ref),
                    run_id=run.run_id if run else None,
                    kind=str(row["artifact_type"]),
                    producer="legacy_v2",
                    producer_version=(run.pipeline_version if run else "unknown"),
                    config_digest=(run.config_digest if run else "unknown"),
                    input_refs=tuple(input_refs),
                    storage_ref=storage_ref,
                    sha256=digest,
                    size_bytes=size_bytes,
                    status=status,
                    metadata=_json_object(row["metadata_json"]),
                    created_at=_legacy_datetime(row["created_at"], "UTC"),
                    legacy_ref=artifact_ref,
                )
            )

    def _prepare_corrections(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        tables: set[str],
        prepared: _PreparedImport,
    ) -> None:
        for table in sorted(_CORRECTION_TABLES & tables):
            for row in _rows(connection, table):
                key = _row_key(row)
                correction_ref = _legacy_ref(namespace, table, key)
                prepared.corrections.append(
                    CorrectionOperation(
                        correction_id=stable_ulid(correction_ref),
                        target_type=f"legacy.{table}",
                        target_id=stable_ulid(namespace, table, "target", key),
                        before_revision=None,
                        patch={
                            "legacy_table": table,
                            "legacy_row": _safe_mapping(row),
                        },
                        actor="legacy_human",
                        created_at=_legacy_datetime(
                            row["created_at"] if "created_at" in row.keys() else None,
                            "UTC",
                            fallback=self._now(),
                        ),
                        legacy_ref=correction_ref,
                    )
                )

    def _prepare_snapshots(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        tables: set[str],
        prepared: _PreparedImport,
    ) -> None:
        for table in sorted(_SNAPSHOT_TABLES & tables):
            rows = list(_rows(connection, table))
            if not rows:
                continue
            prepared.artifacts.append(
                self._row_artifact(
                    namespace,
                    table=table,
                    key="all",
                    rows=rows,
                    run_id=None,
                    input_refs=(),
                    created_at=self._now(),
                    kind="legacy_table_snapshot",
                )
            )

    def _row_artifact(
        self,
        namespace: str,
        *,
        table: str,
        key: str,
        rows: Iterable[sqlite3.Row],
        run_id: str | None,
        input_refs: tuple[str, ...],
        created_at: datetime,
        kind: str,
    ) -> Artifact:
        safe_rows = [_safe_mapping(row) for row in rows]
        payload = json.dumps(
            {"legacy_table": table, "rows": safe_rows},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        stored = self._artifact_store.put_bytes(payload)
        artifact_ref = _legacy_ref(namespace, f"{table}_snapshot", key)
        return Artifact(
            artifact_id=stable_ulid(artifact_ref),
            run_id=run_id,
            kind=kind,
            producer="legacy_v2_importer",
            producer_version="1",
            config_digest=hashlib.sha256(b"legacy_v2_importer:1").hexdigest(),
            input_refs=input_refs,
            storage_ref=stored.storage_key,
            sha256=stored.sha256,
            size_bytes=stored.size_bytes,
            status="active",
            metadata={"legacy_table": table, "row_count": len(safe_rows)},
            created_at=created_at,
            legacy_ref=artifact_ref,
        )

    def _persist(
        self,
        *,
        import_id: str,
        source: Path,
        source_sha256: str,
        source_schema_version: int,
        source_namespace: str,
        prepared: _PreparedImport,
    ) -> LegacyImportResult:
        created: dict[str, int] = {}
        existing: dict[str, int] = {}
        with self._uow_factory() as uow:
            uow.legacy_imports.start(
                import_id,
                source_namespace,
                str(source),
                source_sha256,
                source_schema_version,
            )
            _record(created, existing, "devices", uow.devices.add(prepared.device))
            for session in prepared.sessions:
                added = uow.catalog.add_session(session)
                _record(created, existing, "recording_sessions", added)
                if added:
                    uow.changes.append(
                        "recording_session",
                        session.session_id,
                        session.revision,
                        "upsert",
                        _session_projection(session),
                    )

            actual_asset_ids: dict[str, str] = {}
            for asset in prepared.assets:
                current = uow.catalog.find_asset_by_sha256(asset.sha256)
                if current is not None:
                    actual_asset_ids[asset.asset_id] = current.asset_id
                    _record(created, existing, "audio_assets", False)
                    continue
                added = uow.catalog.add_asset(asset)
                _record(created, existing, "audio_assets", added)
                actual_asset_ids[asset.asset_id] = asset.asset_id
                if added:
                    uow.changes.append(
                        "audio_asset",
                        asset.asset_id,
                        1,
                        "upsert",
                        _asset_projection(asset),
                    )

            actual_replica_ids: dict[str, str] = {}
            for replica in prepared.replicas:
                mapped = replace(
                    replica, asset_id=actual_asset_ids[replica.asset_id]
                )
                added = uow.catalog.add_replica(mapped)
                _record(created, existing, "audio_replicas", added)
                actual_replica_ids[replica.replica_id] = mapped.replica_id

            for segment in prepared.segments:
                mapped = replace(
                    segment,
                    asset_id=actual_asset_ids[segment.asset_id],
                    replica_id=actual_replica_ids[segment.replica_id],
                )
                _record(
                    created,
                    existing,
                    "capture_segments",
                    uow.catalog.add_segment(mapped),
                )
            for manifest in prepared.manifests:
                _record(
                    created,
                    existing,
                    "session_manifests",
                    uow.catalog.add_manifest(manifest),
                )
            for run in prepared.processing_runs:
                added = uow.processing_runs.add(run)
                _record(created, existing, "processing_runs", added)
                if added:
                    uow.changes.append(
                        "processing_run",
                        run.run_id,
                        1,
                        "upsert",
                        _run_projection(run),
                    )
            for artifact in prepared.artifacts:
                _record(
                    created,
                    existing,
                    "artifacts",
                    uow.artifacts.add(artifact),
                )
            for correction in prepared.corrections:
                _record(
                    created,
                    existing,
                    "corrections",
                    uow.corrections.add(correction),
                )
            result = LegacyImportResult(
                import_id=import_id,
                source_database_sha256=source_sha256,
                source_schema_version=source_schema_version,
                created=created,
                existing=existing,
                issues=tuple(prepared.issues),
                unmapped=prepared.unmapped,
            )
            uow.audit.append(
                "legacy_v2.import",
                "system",
                "legacy_import",
                import_id,
                result.as_dict(),
                legacy_ref=f"legacy-import:{import_id}",
            )
            uow.legacy_imports.complete(import_id, result.as_dict())
        return result


def _source_rows(connection: sqlite3.Connection) -> Iterable[sqlite3.Row]:
    return connection.execute(
        """
        SELECT
            ss.id AS mapping_id,
            ss.session_id,
            ss.source_object_id,
            ss.source_instance_id,
            ss.chunk_index,
            ss.session_start_ms,
            ss.session_end_ms,
            ss.source_start_ms,
            ss.source_end_ms,
            ss.session_start_sample,
            so.sha256,
            so.duration_ms,
            so.container,
            so.created_at AS source_created_at,
            si.instance_key,
            si.source_path,
            si.byte_size,
            si.recorded_at AS instance_recorded_at,
            si.timezone AS instance_timezone,
            si.created_at AS instance_created_at
        FROM session_sources ss
        JOIN source_instances si ON si.id = ss.source_instance_id
        JOIN source_objects so ON so.id = ss.source_object_id
        ORDER BY ss.session_id, ss.chunk_index, ss.id
        """
    )


def _processing_run(
    namespace: str, row: sqlite3.Row, legacy_session_id: int
) -> ProcessingRun:
    run_ref = _legacy_ref(namespace, "processing_runs", int(row["id"]))
    status = _processing_status(str(row["status"]))
    started = _legacy_datetime(row["started_at"], "UTC")
    completed = (
        _legacy_datetime(row["completed_at"], "UTC")
        if row["completed_at"] is not None
        else None
    )
    pipeline_version = row["pipeline_version"] or f"legacy:{row['run_kind']}"
    config_digest = str(row["config_sha256"] or "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", config_digest):
        config_digest = hashlib.sha256(str(row["config_json"]).encode("utf-8")).hexdigest()
    return ProcessingRun(
        run_id=stable_ulid(run_ref),
        session_id=stable_ulid(
            _legacy_ref(namespace, "recording_sessions", legacy_session_id)
        ),
        pipeline_version=str(pipeline_version),
        input_revision=1,
        status=status,
        config_digest=config_digest.lower(),
        current_stage=str(row["run_kind"]),
        progress=1.0 if status is ProcessingStatus.SUCCEEDED else 0.0,
        created_at=started,
        updated_at=completed or started,
        completed_at=completed,
        error=row["error"],
        legacy_ref=run_ref,
    )


def _processing_status(value: str) -> ProcessingStatus:
    mapping = {
        "completed": ProcessingStatus.SUCCEEDED,
        "succeeded": ProcessingStatus.SUCCEEDED,
        "failed": ProcessingStatus.FAILED_FINAL,
        "running": ProcessingStatus.RUNNING,
        "pending": ProcessingStatus.QUEUED,
        "queued": ProcessingStatus.QUEUED,
        "cancelled": ProcessingStatus.CANCELLED,
        "interrupted": ProcessingStatus.FAILED_RETRYABLE,
    }
    return mapping.get(value.lower(), ProcessingStatus.STALE)


def _run_session_id(
    row: sqlite3.Row, legacy_recording_to_session: Mapping[int, int]
) -> int | None:
    if row["session_id"] is not None:
        return int(row["session_id"])
    if row["recording_id"] is not None:
        return legacy_recording_to_session.get(int(row["recording_id"]))
    return None


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, timeout=30, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
    ).fetchone()
    return int(row[0])


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


def _audio_format(container: object, source_path: str) -> AudioFormat:
    value = str(container or Path(source_path).suffix.lstrip(".")).lower()
    aliases = {"wave": "wav", "mp4": "m4a"}
    return AudioFormat(aliases.get(value, value))


def _session_projection(session: RecordingSession) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "captured_start": _projection_datetime(session.captured_start),
        "captured_end": (
            _projection_datetime(session.captured_end)
            if session.captured_end is not None
            else None
        ),
        "timezone": session.timezone,
        "state": session.state.value,
        "revision": session.revision,
        "status_code": session.status_code,
        "current_stage": session.current_stage,
        "progress": session.progress,
        "blocking_reason": session.blocking_reason,
    }


def _asset_projection(asset: AudioAsset) -> dict[str, Any]:
    return {
        "asset_id": asset.asset_id,
        "sha256": asset.sha256,
        "size_bytes": asset.size_bytes,
        "duration_ms": asset.duration_ms,
        "format": asset.format.value,
        "media_id": asset.media_id,
    }


def _run_projection(run: ProcessingRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "session_id": run.session_id,
        "pipeline_version": run.pipeline_version,
        "input_revision": run.input_revision,
        "status": run.status.value,
        "current_stage": run.current_stage,
        "progress": run.progress,
        "completed_at": (
            _projection_datetime(run.completed_at)
            if run.completed_at is not None
            else None
        ),
        "error": run.error,
    }


def _projection_datetime(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(value: str) -> str:
    normalized = value.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError("source SHA-256 is invalid")
    return normalized


def _resolve_source_path(value: str, source_database: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (source_database.parent / path).resolve()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _database_fingerprint(path: Path) -> str:
    """Fingerprint the main database and any committed WAL bytes."""
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


def _has_table(connection: sqlite3.Connection, table: str) -> bool:
    return table in _table_names(connection)


def _rows(connection: sqlite3.Connection, table: str) -> Iterable[sqlite3.Row]:
    _safe_identifier(table)
    return connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    _safe_identifier(table)
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _safe_identifier(value: str) -> None:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"unsafe SQLite identifier: {value}")


def _row_key(row: sqlite3.Row) -> str:
    if "id" in row.keys():
        return str(row["id"])
    if "annotation_key" in row.keys():
        return str(row["annotation_key"])
    return hashlib.sha256(
        json.dumps(_safe_mapping(row), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _safe_mapping(row: sqlite3.Row) -> dict[str, Any]:
    return {str(key): _json_safe(row[key]) for key in row.keys()}


def _json_safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {"legacy_raw": str(value)}
    return parsed if isinstance(parsed, dict) else {"legacy_value": parsed}


def _issue(
    prepared: _PreparedImport,
    code: str,
    legacy_ref: str,
    detail: str,
    **metadata: Any,
) -> None:
    prepared.issues.append(
        {"code": code, "legacy_ref": legacy_ref, "detail": detail, **metadata}
    )


def _unmapped(prepared: _PreparedImport, resource_type: str) -> None:
    prepared.unmapped[resource_type] = prepared.unmapped.get(resource_type, 0) + 1


def _record(
    created: dict[str, int], existing: dict[str, int], name: str, added: bool
) -> None:
    target = created if added else existing
    target[name] = target.get(name, 0) + 1


__all__ = ["LegacyV2Importer"]
