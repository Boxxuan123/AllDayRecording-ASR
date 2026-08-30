from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


from allday_asr.infrastructure.sqlite.connection import connect_sqlite
from allday_asr.infrastructure.sqlite.migration_runner import (
    LATEST_SCHEMA_VERSION,
    MigrationHooks,
    MigrationRunner,
)
from allday_asr.infrastructure.sqlite.migrations import (
    MIGRATIONS,
    SCHEMA,
    V4_PROCESSING_RUN_COLUMNS,
    V5_GUARD_SQL,
    V5_PREDICTION_SET_COLUMNS,
    V7_GUARD_SQL,
)
from allday_asr.infrastructure.sqlite.repositories import (
    ActionRepository,
    AsrRepository,
    DiarizationRepository,
    EvaluationRepository,
    IdentityRepository,
    RunRepository,
    SemanticRepository,
    SessionRepository,
)
from allday_asr.infrastructure.sqlite.source_fingerprint import (
    canonical_source_fingerprint as _canonical_source_fingerprint,
)


__all__ = [
    "Database",
    "LATEST_SCHEMA_VERSION",
    "MIGRATIONS",
    "SCHEMA",
    "V5_GUARD_SQL",
    "V7_GUARD_SQL",
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_path(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


def _refresh_session_instance_backup_statuses(
    connection: sqlite3.Connection, session_id: int
) -> None:
    now = utc_now()
    connection.execute(
        """
        UPDATE source_instances AS si
        SET backup_status = CASE
                WHEN EXISTS (
                    SELECT 1
                    FROM session_backup_files sbf
                    JOIN session_backups sb ON sb.id = sbf.backup_id
                    WHERE sbf.source_instance_id = si.id
                      AND sb.status = 'verified'
                      AND sb.restore_verified_at IS NOT NULL
                      AND sb.storage_kind IN ('independent_device', 'network')
                ) THEN 'verified'
                WHEN EXISTS (
                    SELECT 1
                    FROM session_backup_files sbf
                    JOIN session_backups sb ON sb.id = sbf.backup_id
                    WHERE sbf.source_instance_id = si.id
                      AND sb.status = 'verified'
                ) THEN 'pending'
                WHEN EXISTS (
                    SELECT 1
                    FROM session_backup_files sbf
                    JOIN session_backups sb ON sb.id = sbf.backup_id
                    WHERE sbf.source_instance_id = si.id
                ) THEN 'failed'
                ELSE 'not_configured'
            END,
            updated_at = ?
        WHERE si.id IN (
            SELECT source_instance_id
            FROM session_sources
            WHERE session_id = ?
        )
        """,
        (now, session_id),
    )


def _ensure_v4_processing_run_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(processing_runs)")
    }
    for name, declaration in V4_PROCESSING_RUN_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE processing_runs ADD COLUMN {name} {declaration}"
            )


def _ensure_v5_prediction_set_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(benchmark_prediction_sets)")
    }
    for name, declaration in V5_PREDICTION_SET_COLUMNS.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE benchmark_prediction_sets ADD COLUMN {name} {declaration}"
            )


def _backfill_v2_source_graph(
    connection: sqlite3.Connection, recording_id: int | None = None
) -> None:
    where = "WHERE id = ?" if recording_id is not None else ""
    params: tuple[Any, ...] = (recording_id,) if recording_id is not None else ()
    recordings = list(
        connection.execute(f"SELECT * FROM recordings {where} ORDER BY id", params)
    )
    now = utc_now()
    for recording in recordings:
        path = Path(str(recording["source_path"]))
        byte_size = path.stat().st_size if path.is_file() else None
        container = path.suffix.lower().lstrip(".") or None
        connection.execute(
            """
            INSERT INTO source_objects (
                sha256, source_path, original_filename, byte_size, container,
                codec, sample_rate, channels, bit_rate, encoder, device,
                recorded_at, timezone, duration_ms, ingest_method,
                storage_class, integrity_status, backup_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual_file',
                      'original_permanent', 'unverified', 'not_configured', ?, ?)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (
                recording["sha256"],
                recording["source_path"],
                path.name,
                byte_size,
                container,
                recording["codec"],
                recording["sample_rate"],
                recording["channels"],
                recording["bit_rate"],
                recording["encoder"],
                recording["device"],
                recording["recorded_at"],
                recording["timezone"],
                recording["duration_ms"],
                recording["created_at"] or now,
                now,
            ),
        )
        source = connection.execute(
            "SELECT * FROM source_objects WHERE sha256 = ?", (recording["sha256"],)
        ).fetchone()
        session_key = f"legacy-recording:{int(recording['id'])}"
        connection.execute(
            """
            INSERT INTO recording_sessions (
                session_key, legacy_recording_id, device, recorded_at, timezone,
                duration_ms, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(session_key) DO NOTHING
            """,
            (
                session_key,
                recording["id"],
                recording["device"],
                recording["recorded_at"],
                recording["timezone"],
                recording["duration_ms"],
                recording["created_at"] or now,
                now,
            ),
        )
        session = connection.execute(
            "SELECT * FROM recording_sessions WHERE session_key = ?", (session_key,)
        ).fetchone()
        existing_mapping = connection.execute(
            """
            SELECT id, source_instance_id FROM session_sources
            WHERE session_id = ? AND chunk_index = 0
            """,
            (session["id"],),
        ).fetchone()
        if existing_mapping is None:
            instance_key = f"legacy-recording:{int(recording['id'])}"
            connection.execute(
                """
                INSERT INTO source_instances (
                    instance_key, source_object_id, source_path, original_filename,
                    byte_size, recorded_at, timezone, device, ingest_method,
                    sample_rate, sample_count, integrity_status, last_verified_at,
                    backup_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'manual_file', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_key) DO NOTHING
                """,
                (
                    instance_key,
                    source["id"],
                    recording["source_path"],
                    path.name,
                    byte_size or 0,
                    recording["recorded_at"],
                    recording["timezone"],
                    recording["device"],
                    recording["sample_rate"],
                    (
                        round(
                            int(recording["duration_ms"])
                            * int(recording["sample_rate"])
                            / 1000
                        )
                        if recording["sample_rate"] is not None
                        else None
                    ),
                    source["integrity_status"],
                    source["last_verified_at"],
                    source["backup_status"],
                    recording["created_at"] or now,
                    now,
                ),
            )
            source_instance = connection.execute(
                "SELECT * FROM source_instances WHERE instance_key = ?",
                (instance_key,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO session_sources (
                    session_id, source_object_id, source_instance_id,
                    chunk_index, session_start_ms, session_end_ms,
                    source_start_ms, source_end_ms, continuity_status, created_at
                ) VALUES (?, ?, ?, 0, 0, ?, 0, ?, 'single', ?)
                """,
                (
                    session["id"],
                    source["id"],
                    source_instance["id"],
                    recording["duration_ms"],
                    recording["duration_ms"],
                    now,
                ),
            )
        if str(session["status"]) == "active":
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = 'closed', updated_at = ?
                WHERE id = ?
                """,
                (now, session["id"]),
            )

    runs = list(
        connection.execute(
            """
            SELECT pr.id AS run_id, rs.id AS session_id
            FROM processing_runs pr
            JOIN recording_sessions rs ON rs.legacy_recording_id = pr.recording_id
            ORDER BY pr.id
            """
        )
    )
    for run in runs:
        inputs = list(
            connection.execute(
                """
                SELECT ss.*, so.sha256, si.instance_key
                FROM session_sources ss
                JOIN source_objects so ON so.id = ss.source_object_id
                JOIN source_instances si ON si.id = ss.source_instance_id
                WHERE ss.session_id = ?
                ORDER BY ss.chunk_index
                """,
                (run["session_id"],),
            )
        )
        if not inputs:
            continue
        fingerprint = _canonical_source_fingerprint(inputs)
        connection.execute(
            """
            UPDATE processing_runs
            SET input_fingerprint = COALESCE(input_fingerprint, ?),
                session_id = COALESCE(session_id, ?)
            WHERE id = ?
            """,
            (fingerprint, run["session_id"], run["run_id"]),
        )
        connection.executemany(
            """
            INSERT INTO processing_run_inputs (
                run_id, position, source_object_id, source_instance_id, source_sha256,
                session_start_ms, session_end_ms, source_start_ms, source_end_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, position) DO NOTHING
            """,
            [
                (
                    run["run_id"],
                    position,
                    row["source_object_id"],
                    row["source_instance_id"],
                    row["sha256"],
                    row["session_start_ms"],
                    row["session_end_ms"],
                    row["source_start_ms"],
                    row["source_end_ms"],
                )
                for position, row in enumerate(inputs)
            ],
        )


class Database:
    """Compatibility facade with an explicit initialization lifecycle."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.sessions = SessionRepository(
            self.connect,
            now=utc_now,
            get_recording=self.get_recording,
        )
        self.runs = RunRepository(
            self.connect,
            sessions=self.sessions,
            now=utc_now,
            get_recording=self.get_recording,
        )
        self.semantic = SemanticRepository(
            self.connect,
            runs=self.runs,
            sessions=self.sessions,
            now=utc_now,
        )
        self.asr = AsrRepository(
            self.connect,
            runs=self.runs,
            now=utc_now,
        )
        self.diarization = DiarizationRepository(
            self.connect,
            runs=self.runs,
            sessions=self.sessions,
            now=utc_now,
        )
        self.identity = IdentityRepository(
            self.connect,
            runs=self.runs,
            sessions=self.sessions,
            now=utc_now,
        )
        self.evaluation = EvaluationRepository(
            self.connect,
            sessions=self.sessions,
            now=utc_now,
            get_recording=self.get_recording,
        )
        self.actions = ActionRepository(
            self.connect,
            now=utc_now,
        )

    @classmethod
    def open(cls, path: Path) -> Database:
        """Create a facade and explicitly initialize or migrate its database."""
        database = cls(path)
        database.initialize()
        return database

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with connect_sqlite(self.path) as connection:
            yield connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        MigrationRunner(
            self.path,
            self.connect,
            hooks=MigrationHooks(
                ensure_v4_processing_run_columns=_ensure_v4_processing_run_columns,
                ensure_v5_prediction_set_columns=_ensure_v5_prediction_set_columns,
                backfill_source_graph=_backfill_v2_source_graph,
            ),
            now=utc_now,
        ).initialize()

    def schema_version(self) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
            return int(row["version"] or 0)

    def find_recording_by_hash(self, sha256: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM recordings WHERE sha256 = ?", (sha256,)
            ).fetchone()

    def create_recording(self, values: dict[str, Any]) -> sqlite3.Row:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO recordings (
                    source_path, sha256, device, recorded_at, timezone, duration_ms,
                    codec, sample_rate, channels, bit_rate, encoder, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["source_path"],
                    values["sha256"],
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("codec"),
                    values.get("sample_rate"),
                    values.get("channels"),
                    values.get("bit_rate"),
                    values.get("encoder"),
                    now,
                    now,
                ),
            )
            recording_id = int(cursor.lastrowid)
        with self.connect() as connection:
            _backfill_v2_source_graph(connection, recording_id=recording_id)
        return self.get_recording(recording_id)

    def get_recording(self, recording_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM recordings WHERE id = ?", (recording_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"录音 {recording_id} 不存在")
        return row

    def list_recordings(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM recordings ORDER BY id DESC"))

    def get_source_object(self, source_object_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_objects WHERE id = ?", (source_object_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"原始音频对象 {source_object_id} 不存在")
        return row

    def list_source_objects(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute("SELECT * FROM source_objects ORDER BY id"))

    def get_source_instance(self, source_instance_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_instances WHERE id = ?", (source_instance_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"原始音频实例 {source_instance_id} 不存在")
        return row

    def list_source_instances(self, session_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(
                    connection.execute(
                        """
                        SELECT si.*, so.sha256, so.codec, so.channels, so.container
                        FROM source_instances si
                        JOIN source_objects so ON so.id = si.source_object_id
                        ORDER BY si.id
                        """
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT si.*, so.sha256, so.codec, so.channels, so.container,
                           ss.chunk_index, ss.session_start_ms, ss.session_end_ms,
                           ss.continuity_status
                    FROM session_sources ss
                    JOIN source_instances si ON si.id = ss.source_instance_id
                    JOIN source_objects so ON so.id = si.source_object_id
                    WHERE ss.session_id = ?
                    ORDER BY ss.chunk_index
                    """,
                    (session_id,),
                )
            )

    def update_source_instance_integrity(
        self,
        source_instance_id: int,
        *,
        status: str,
        verified_at: str | None,
    ) -> None:
        if status not in {"unverified", "verified", "missing", "mismatch", "error"}:
            raise ValueError(f"无效的原始音频实例完整性状态：{status}")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE source_instances
                SET integrity_status = ?, last_verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, verified_at, utc_now(), source_instance_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"原始音频实例 {source_instance_id} 不存在")

    def create_source_object(self, values: dict[str, Any]) -> sqlite3.Row:
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM source_objects WHERE sha256 = ?", (values["sha256"],)
            ).fetchone()
            if existing is not None:
                return existing
            now = utc_now()
            cursor = connection.execute(
                """
                INSERT INTO source_objects (
                    sha256, source_path, original_filename, byte_size, container,
                    codec, sample_rate, channels, bit_rate, encoder, device,
                    recorded_at, timezone, duration_ms, ingest_method,
                    storage_class, integrity_status, backup_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'original_permanent', 'unverified', 'not_configured', ?, ?)
                """,
                (
                    values["sha256"],
                    values["source_path"],
                    values.get("original_filename")
                    or Path(values["source_path"]).name,
                    values.get("byte_size"),
                    values.get("container"),
                    values.get("codec"),
                    values.get("sample_rate"),
                    values.get("channels"),
                    values.get("bit_rate"),
                    values.get("encoder"),
                    values.get("device"),
                    values["recorded_at"],
                    values["timezone"],
                    values["duration_ms"],
                    values.get("ingest_method", "manual_file"),
                    now,
                    now,
                ),
            )
            source_object_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM source_objects WHERE id = ?", (source_object_id,)
            ).fetchone()

    def get_recording_session(self, session_id: int) -> sqlite3.Row:
        return self.sessions.get_recording_session(session_id)

    def find_recording_session(self, session_key: str) -> sqlite3.Row | None:
        return self.sessions.find_recording_session(session_key)

    def get_session_manifest(self, session_id: int) -> sqlite3.Row | None:
        return self.sessions.get_session_manifest(session_id)

    def find_session_manifest_by_hash(
        self, manifest_sha256: str
    ) -> sqlite3.Row | None:
        return self.sessions.find_session_manifest_by_hash(manifest_sha256)

    def create_verified_session_backup(
        self,
        values: dict[str, Any],
        files: Sequence[dict[str, Any]],
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        self.get_recording_session(session_id)
        expected_input_fingerprint = self.session_input_fingerprint(session_id)
        if str(values["input_fingerprint"]) != expected_input_fingerprint:
            raise ValueError("会话备份输入指纹与当前不可变会话不一致")
        storage_kind = str(values["storage_kind"])
        if storage_kind not in {"independent_device", "network", "same_device_test"}:
            raise ValueError(f"无效的备份存储类型：{storage_kind}")
        if not files:
            raise ValueError("会话备份没有文件证据")
        session_sources = self.list_session_sources(session_id)
        source_evidence = {
            int(row["source_instance_id"]): (
                str(row["sha256"]),
                int(row["instance_byte_size"]),
            )
            for row in session_sources
        }
        source_ids = set(source_evidence)
        supplied_source_ids = [
            int(item["source_instance_id"])
            for item in files
            if item["file_kind"] == "source_audio"
        ]
        if len(supplied_source_ids) != len(set(supplied_source_ids)):
            raise ValueError("会话备份包含重复的原音实例")
        if set(supplied_source_ids) != source_ids:
            raise ValueError("会话备份没有精确覆盖全部原音实例")
        manifest = self.get_session_manifest(session_id)
        manifest_expected = manifest is not None
        manifest_files = [
            item for item in files if item["file_kind"] == "capture_manifest"
        ]
        if len(manifest_files) != (1 if manifest_expected else 0):
            raise ValueError("会话备份的原始采集清单覆盖不正确")
        relative_paths: set[str] = set()
        for position, item in enumerate(files):
            if int(item["position"]) != position:
                raise ValueError("会话备份文件 position 必须连续")
            file_kind = str(item["file_kind"])
            if file_kind not in {"source_audio", "capture_manifest"}:
                raise ValueError("会话备份文件类型无效")
            relative_path = str(item["relative_path"])
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("会话备份文件必须使用安全的相对路径")
            if relative_path in relative_paths:
                raise ValueError("会话备份包含重复的相对路径")
            relative_paths.add(relative_path)
            if int(item["byte_size"]) < 0:
                raise ValueError("会话备份文件大小不能为负数")
            if file_kind == "source_audio":
                expected = source_evidence[int(item["source_instance_id"])]
            else:
                assert manifest is not None
                expected = (
                    str(manifest["manifest_sha256"]),
                    int(manifest["byte_size"]),
                )
            actual = (str(item["sha256"]), int(item["byte_size"]))
            if actual != expected:
                raise ValueError("会话备份文件证据与不可变原始输入不一致")

        now = utc_now()
        backup_key = str(values["backup_key"])
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM session_backups WHERE backup_key = ?",
                (backup_key,),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """
                    INSERT INTO session_backups (
                        backup_key, session_id, storage_kind, backup_path,
                        input_fingerprint, backup_manifest_sha256,
                        file_count, total_bytes, status, created_at,
                        verified_at, last_checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?, ?, ?)
                    """,
                    (
                        backup_key,
                        session_id,
                        storage_kind,
                        values["backup_path"],
                        values["input_fingerprint"],
                        values["backup_manifest_sha256"],
                        len(files),
                        sum(int(item["byte_size"]) for item in files),
                        now,
                        now,
                        now,
                    ),
                )
                backup_id = int(cursor.lastrowid)
                connection.executemany(
                    """
                    INSERT INTO session_backup_files (
                        backup_id, position, file_kind, source_instance_id,
                        relative_path, sha256, byte_size
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            backup_id,
                            item["position"],
                            item["file_kind"],
                            item.get("source_instance_id"),
                            item["relative_path"],
                            item["sha256"],
                            item["byte_size"],
                        )
                        for item in files
                    ],
                )
            else:
                backup_id = int(existing["id"])
                immutable_values = {
                    "session_id": session_id,
                    "storage_kind": storage_kind,
                    "backup_path": str(values["backup_path"]),
                    "input_fingerprint": str(values["input_fingerprint"]),
                    "backup_manifest_sha256": str(
                        values["backup_manifest_sha256"]
                    ),
                    "file_count": len(files),
                    "total_bytes": sum(int(item["byte_size"]) for item in files),
                }
                if any(existing[key] != value for key, value in immutable_values.items()):
                    raise ValueError("同一 backup_key 的不可变备份元数据不一致")
                existing_files = list(
                    connection.execute(
                        """
                        SELECT position, file_kind, source_instance_id,
                               relative_path, sha256, byte_size
                        FROM session_backup_files
                        WHERE backup_id = ? ORDER BY position
                        """,
                        (backup_id,),
                    )
                )
                canonical_existing = [tuple(row) for row in existing_files]
                canonical_supplied = [
                    (
                        item["position"],
                        item["file_kind"],
                        item.get("source_instance_id"),
                        item["relative_path"],
                        item["sha256"],
                        item["byte_size"],
                    )
                    for item in files
                ]
                if canonical_existing != canonical_supplied:
                    raise ValueError("同一 backup_key 的文件证据不一致")
                connection.execute(
                    """
                    UPDATE session_backups
                    SET status = 'verified', verified_at = ?,
                        last_checked_at = ?, error = NULL
                    WHERE id = ?
                    """,
                    (now, now, backup_id),
                )
            _refresh_session_instance_backup_statuses(connection, session_id)
            return connection.execute(
                "SELECT * FROM session_backups WHERE id = ?", (backup_id,)
            ).fetchone()

    def get_session_backup(self, backup_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_backups WHERE id = ?", (backup_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"会话备份 {backup_id} 不存在")
        return row

    def list_session_backups(self, session_id: int) -> list[sqlite3.Row]:
        self.get_recording_session(session_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    "SELECT * FROM session_backups WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            )

    def list_session_backup_files(self, backup_id: int) -> list[sqlite3.Row]:
        self.get_session_backup(backup_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM session_backup_files
                    WHERE backup_id = ? ORDER BY position
                    """,
                    (backup_id,),
                )
            )

    def mark_session_backup_restore_verified(self, backup_id: int) -> sqlite3.Row:
        backup = self.get_session_backup(backup_id)
        if str(backup["status"]) != "verified":
            raise ValueError("只有文件校验通过的备份才能完成恢复演练")
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE session_backups
                SET restore_verified_at = ?, last_checked_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, now, backup_id),
            )
            _refresh_session_instance_backup_statuses(
                connection, int(backup["session_id"])
            )
            return connection.execute(
                "SELECT * FROM session_backups WHERE id = ?", (backup_id,)
            ).fetchone()

    def mark_session_backup_failed(self, backup_id: int, error: str) -> sqlite3.Row:
        backup = self.get_session_backup(backup_id)
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE session_backups
                SET status = 'failed', restore_verified_at = NULL,
                    last_checked_at = ?, error = ?
                WHERE id = ?
                """,
                (now, error[:2000], backup_id),
            )
            _refresh_session_instance_backup_statuses(
                connection, int(backup["session_id"])
            )
            return connection.execute(
                "SELECT * FROM session_backups WHERE id = ?", (backup_id,)
            ).fetchone()

    def get_session_for_recording(self, recording_id: int) -> sqlite3.Row:
        return self.sessions.get_session_for_recording(recording_id)

    def list_recording_sessions(self) -> list[sqlite3.Row]:
        return self.sessions.list_recording_sessions()

    def create_recording_session(self, values: dict[str, Any]) -> sqlite3.Row:
        return self.sessions.create_recording_session(values)

    def add_session_source(self, values: dict[str, Any]) -> sqlite3.Row:
        session = self.get_recording_session(int(values["session_id"]))
        if str(session["status"]) != "active":
            raise ValueError("关闭的录音会话不能追加原始音频")
        source = self.get_source_object(int(values["source_object_id"]))
        created_at = utc_now()
        with self.connect() as connection:
            source_instance_id = values.get("source_instance_id")
            if source_instance_id is None:
                instance_key = str(
                    values.get("instance_key")
                    or (
                        f"manual-session:{int(values['session_id'])}:"
                        f"chunk:{int(values['chunk_index'])}"
                    )
                )
                existing_instance = connection.execute(
                    "SELECT * FROM source_instances WHERE instance_key = ?",
                    (instance_key,),
                ).fetchone()
                if existing_instance is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO source_instances (
                            instance_key, source_object_id, source_path,
                            original_filename, byte_size, recorded_at, timezone,
                            device, ingest_method, sample_rate, sample_count,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            instance_key,
                            source["id"],
                            source["source_path"],
                            source["original_filename"],
                            int(source["byte_size"] or 0),
                            source["recorded_at"],
                            source["timezone"],
                            source["device"],
                            source["ingest_method"],
                            source["sample_rate"],
                            (
                                round(
                                    int(source["duration_ms"])
                                    * int(source["sample_rate"])
                                    / 1000
                                )
                                if source["sample_rate"] is not None
                                else None
                            ),
                            created_at,
                            created_at,
                        ),
                    )
                    source_instance_id = int(cursor.lastrowid)
                else:
                    source_instance_id = int(existing_instance["id"])
            else:
                instance = connection.execute(
                    "SELECT * FROM source_instances WHERE id = ?",
                    (source_instance_id,),
                ).fetchone()
                if instance is None:
                    raise KeyError(f"原始音频实例 {source_instance_id} 不存在")
                if int(instance["source_object_id"]) != int(source["id"]):
                    raise ValueError("原始音频实例与内容对象不匹配")
            cursor = connection.execute(
                """
                INSERT INTO session_sources (
                    session_id, source_object_id, source_instance_id, chunk_index,
                    session_start_ms, session_end_ms, source_start_ms,
                    source_end_ms, session_start_sample, session_end_sample,
                    source_start_sample, source_end_sample, timeline_sample_rate,
                    continuity_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["session_id"],
                    values["source_object_id"],
                    source_instance_id,
                    values["chunk_index"],
                    values["session_start_ms"],
                    values["session_end_ms"],
                    values.get("source_start_ms", 0),
                    values["source_end_ms"],
                    values.get("session_start_sample"),
                    values.get("session_end_sample"),
                    values.get("source_start_sample"),
                    values.get("source_end_sample"),
                    values.get("timeline_sample_rate"),
                    values.get("continuity_status", "unchecked"),
                    created_at,
                ),
            )
            source_id = int(cursor.lastrowid)
            connection.execute(
                """
                UPDATE recording_sessions
                SET duration_ms = MAX(duration_ms, ?), updated_at = ?
                WHERE id = ?
                """,
                (values["session_end_ms"], created_at, values["session_id"]),
            )
            return connection.execute(
                "SELECT * FROM session_sources WHERE id = ?", (source_id,)
            ).fetchone()

    def list_session_sources(self, session_id: int) -> list[sqlite3.Row]:
        return self.sessions.list_session_sources(session_id)

    def session_input_fingerprint(self, session_id: int) -> str:
        return self.sessions.session_input_fingerprint(session_id)

    def import_closed_session(
        self,
        session_values: dict[str, Any],
        manifest_values: dict[str, Any],
        chunks: Sequence[dict[str, Any]],
    ) -> tuple[sqlite3.Row, bool]:
        """Atomically register a validated immutable manifest and all source instances."""
        if not chunks:
            raise ValueError("录音会话清单没有音频分片")
        session_key = str(session_values["session_key"])
        manifest_sha256 = str(manifest_values["manifest_sha256"])
        now = utc_now()
        with self.connect() as connection:
            existing_manifest = connection.execute(
                """
                SELECT sm.*, rs.session_key
                FROM session_manifests sm
                JOIN recording_sessions rs ON rs.id = sm.session_id
                WHERE sm.manifest_sha256 = ?
                """,
                (manifest_sha256,),
            ).fetchone()
            if existing_manifest is not None:
                if str(existing_manifest["session_key"]) != session_key:
                    raise ValueError("相同清单哈希已经属于另一个录音会话")
                session = connection.execute(
                    "SELECT * FROM recording_sessions WHERE id = ?",
                    (existing_manifest["session_id"],),
                ).fetchone()
                return session, False

            existing_session = connection.execute(
                "SELECT * FROM recording_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if existing_session is not None:
                raise ValueError("录音会话键已存在，但清单哈希不同")

            cursor = connection.execute(
                """
                INSERT INTO recording_sessions (
                    session_key, legacy_recording_id, device, recorded_at,
                    timezone, duration_ms, status, created_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    session_key,
                    session_values.get("device"),
                    session_values["recorded_at"],
                    session_values["timezone"],
                    session_values["duration_ms"],
                    now,
                    now,
                ),
            )
            session_id = int(cursor.lastrowid)

            for chunk in chunks:
                source_values = dict(chunk["source"])
                instance_values = dict(chunk["instance"])
                mapping = dict(chunk["mapping"])
                source = connection.execute(
                    "SELECT * FROM source_objects WHERE sha256 = ?",
                    (source_values["sha256"],),
                ).fetchone()
                if source is None:
                    source_cursor = connection.execute(
                        """
                        INSERT INTO source_objects (
                            sha256, source_path, original_filename, byte_size,
                            container, codec, sample_rate, channels, bit_rate,
                            encoder, device, recorded_at, timezone, duration_ms,
                            ingest_method, storage_class, integrity_status,
                            last_verified_at, backup_status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'original_permanent', 'verified', ?,
                                  'not_configured', ?, ?)
                        """,
                        (
                            source_values["sha256"],
                            source_values["source_path"],
                            source_values["original_filename"],
                            source_values["byte_size"],
                            source_values.get("container"),
                            source_values.get("codec"),
                            source_values.get("sample_rate"),
                            source_values.get("channels"),
                            source_values.get("bit_rate"),
                            source_values.get("encoder"),
                            source_values.get("device"),
                            source_values["recorded_at"],
                            source_values["timezone"],
                            source_values["duration_ms"],
                            source_values.get("ingest_method", "watch_manual_sync"),
                            now,
                            now,
                            now,
                        ),
                    )
                    source = connection.execute(
                        "SELECT * FROM source_objects WHERE id = ?",
                        (int(source_cursor.lastrowid),),
                    ).fetchone()
                instance_key = str(instance_values["instance_key"])
                if connection.execute(
                    "SELECT 1 FROM source_instances WHERE instance_key = ?",
                    (instance_key,),
                ).fetchone() is not None:
                    raise ValueError(f"原始音频实例键重复：{instance_key}")
                instance_cursor = connection.execute(
                    """
                    INSERT INTO source_instances (
                        instance_key, source_object_id, source_path,
                        original_filename, byte_size, recorded_at, timezone,
                        device, ingest_method, sample_rate, sample_count,
                        integrity_status, last_verified_at, backup_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?,
                              'not_configured', ?, ?)
                    """,
                    (
                        instance_key,
                        source["id"],
                        instance_values["source_path"],
                        instance_values["original_filename"],
                        instance_values["byte_size"],
                        instance_values["recorded_at"],
                        instance_values["timezone"],
                        instance_values.get("device"),
                        instance_values.get("ingest_method", "watch_manual_sync"),
                        instance_values.get("sample_rate"),
                        instance_values.get("sample_count"),
                        now,
                        now,
                        now,
                    ),
                )
                source_instance_id = int(instance_cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO session_sources (
                        session_id, source_object_id, source_instance_id,
                        chunk_index, session_start_ms, session_end_ms,
                        source_start_ms, source_end_ms,
                        session_start_sample, session_end_sample,
                        source_start_sample, source_end_sample,
                        timeline_sample_rate, continuity_status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        source["id"],
                        source_instance_id,
                        mapping["chunk_index"],
                        mapping["session_start_ms"],
                        mapping["session_end_ms"],
                        mapping.get("source_start_ms", 0),
                        mapping["source_end_ms"],
                        mapping.get("session_start_sample"),
                        mapping.get("session_end_sample"),
                        mapping.get("source_start_sample", 0),
                        mapping.get("source_end_sample"),
                        mapping.get("timeline_sample_rate"),
                        mapping.get("continuity_status", "unchecked"),
                        now,
                    ),
                )

            connection.execute(
                """
                INSERT INTO session_manifests (
                    session_id, manifest_format, manifest_path,
                    manifest_sha256, byte_size, parser_version,
                    summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    manifest_values["manifest_format"],
                    manifest_values["manifest_path"],
                    manifest_sha256,
                    manifest_values["byte_size"],
                    manifest_values["parser_version"],
                    json.dumps(
                        manifest_values.get("summary", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = 'closed', updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            session = connection.execute(
                "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            return session, True

    def record_source_integrity_audit(
        self,
        source_object_id: int,
        *,
        status: str,
        actual_sha256: str | None,
        actual_byte_size: int | None,
        details: dict[str, Any],
    ) -> int:
        source = self.get_source_object(source_object_id)
        checked_at = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO source_integrity_audits (
                    source_object_id, status, expected_sha256, actual_sha256,
                    expected_byte_size, actual_byte_size, details_json, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_object_id,
                    status,
                    source["sha256"],
                    actual_sha256,
                    source["byte_size"],
                    actual_byte_size,
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    checked_at,
                ),
            )
            connection.execute(
                """
                UPDATE source_objects
                SET integrity_status = ?, last_verified_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, checked_at, checked_at, source_object_id),
            )
            return int(cursor.lastrowid)

    def list_source_integrity_audits(self, source_object_id: int) -> list[sqlite3.Row]:
        self.get_source_object(source_object_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM source_integrity_audits
                    WHERE source_object_id = ? ORDER BY id
                    """,
                    (source_object_id,),
                )
            )

    def update_recording(self, recording_id: int, **values: Any) -> None:
        if not values:
            return
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{column} = ?" for column in values)
        params = [*values.values(), recording_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE recordings SET {assignments} WHERE id = ?", params
            )

    def set_stage(
        self,
        recording_id: int,
        stage: str,
        status: str,
        *,
        model_id: str | None = None,
        model_version: str | None = None,
        error: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now()
        started_at = now if status == "running" else None
        completed_at = now if status in {"completed", "partial", "failed"} else None
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT started_at FROM processing_stages WHERE recording_id = ? AND stage = ?",
                (recording_id, stage),
            ).fetchone()
            preserved_start = existing["started_at"] if existing else None
            connection.execute(
                """
                INSERT INTO processing_stages (
                    recording_id, stage, status, model_id, model_version,
                    started_at, completed_at, error, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(recording_id, stage) DO UPDATE SET
                    status = excluded.status,
                    model_id = COALESCE(excluded.model_id, processing_stages.model_id),
                    model_version = COALESCE(excluded.model_version, processing_stages.model_version),
                    started_at = COALESCE(excluded.started_at, processing_stages.started_at),
                    completed_at = excluded.completed_at,
                    error = excluded.error,
                    details_json = excluded.details_json
                """,
                (
                    recording_id,
                    stage,
                    status,
                    model_id,
                    model_version,
                    started_at or preserved_start,
                    completed_at,
                    error,
                    json.dumps(details, ensure_ascii=False) if details else None,
                ),
            )

    def get_stage(self, recording_id: int, stage: str) -> sqlite3.Row | None:
        """Return the persisted status for one processing stage, if present."""
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT * FROM processing_stages
                WHERE recording_id = ? AND stage = ?
                """,
                (recording_id, stage),
            ).fetchone()

    def list_stages(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM processing_stages
                    WHERE recording_id = ? ORDER BY stage
                    """,
                    (recording_id,),
                )
            )

    def start_processing_run(
        self,
        recording_id: int | None,
        *,
        session_id: int | None = None,
        run_kind: str,
        config: dict[str, Any],
        config_sha256: str,
        model_manifest: dict[str, Any] | None = None,
        pipeline_version: str | None = None,
        code_version: str | None = None,
        parent_run_id: int | None = None,
    ) -> int:
        return self.runs.start_processing_run(
            recording_id,
            session_id=session_id,
            run_kind=run_kind,
            config=config,
            config_sha256=config_sha256,
            model_manifest=model_manifest,
            pipeline_version=pipeline_version,
            code_version=code_version,
            parent_run_id=parent_run_id,
        )

    def finish_processing_run(
        self,
        run_id: int,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        artifacts: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.runs.finish_processing_run(
            run_id,
            status=status,
            summary=summary,
            artifacts=artifacts,
            error=error,
        )

    def update_processing_run_progress(
        self, run_id: int, summary: dict[str, Any]
    ) -> None:
        self.runs.update_processing_run_progress(run_id, summary)

    def resume_processing_run(self, run_id: int) -> sqlite3.Row:
        return self.runs.resume_processing_run(run_id)

    def list_processing_runs(self, recording_id: int) -> list[sqlite3.Row]:
        return self.runs.list_processing_runs(recording_id)

    def list_session_processing_runs(
        self, session_id: int
    ) -> list[sqlite3.Row]:
        return self.runs.list_session_processing_runs(session_id)

    def create_semantic_snapshot(
        self,
        run_id: int,
        exchange: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        return self.semantic.create_semantic_snapshot(run_id, exchange, candidates)

    def get_semantic_exchange(self, run_id: int) -> sqlite3.Row:
        return self.semantic.get_semantic_exchange(run_id)

    def list_semantic_candidates(self, run_id: int) -> list[sqlite3.Row]:
        return self.semantic.list_semantic_candidates(run_id)

    def get_semantic_candidate(self, candidate_id: int) -> sqlite3.Row:
        return self.semantic.get_semantic_candidate(candidate_id)

    def create_semantic_candidate_revision(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        body: str | None = None,
        note: str | None = None,
    ) -> sqlite3.Row:
        return self.semantic.create_semantic_candidate_revision(
            candidate_id,
            status=status,
            title=title,
            body=body,
            note=note,
        )

    def list_semantic_candidate_revisions(
        self, candidate_id: int
    ) -> list[sqlite3.Row]:
        return self.semantic.list_semantic_candidate_revisions(candidate_id)

    def upsert_v2d1_candidate_review(
        self, run_id: int, values: dict[str, Any]
    ) -> sqlite3.Row:
        return self.identity.upsert_v2d1_candidate_review(run_id, values)

    def list_v2d1_candidate_reviews(self, run_id: int) -> list[sqlite3.Row]:
        return self.identity.list_v2d1_candidate_reviews(run_id)

    def upsert_v2d1_review_completion(
        self,
        run_id: int,
        *,
        candidate_count: int,
        reviewed_count: int,
    ) -> sqlite3.Row:
        return self.identity.upsert_v2d1_review_completion(
            run_id,
            candidate_count=candidate_count,
            reviewed_count=reviewed_count,
        )

    def get_v2d1_review_completion(self, run_id: int) -> sqlite3.Row | None:
        return self.identity.get_v2d1_review_completion(run_id)

    def upsert_v2d1_identity_label(
        self, run_id: int, candidate_id: str, identity_label: str
    ) -> sqlite3.Row:
        return self.identity.upsert_v2d1_identity_label(
            run_id,
            candidate_id,
            identity_label,
        )

    def delete_v2d1_identity_label(self, run_id: int, candidate_id: str) -> None:
        return self.identity.delete_v2d1_identity_label(run_id, candidate_id)

    def list_v2d1_identity_labels(self, run_id: int) -> list[sqlite3.Row]:
        return self.identity.list_v2d1_identity_labels(run_id)

    def upsert_manual_identity_annotation(
        self, values: dict[str, Any]
    ) -> sqlite3.Row:
        return self.identity.upsert_manual_identity_annotation(values)

    def list_manual_identity_annotations(
        self,
        session_id: int,
        *,
        diarization_run_id: int | None = None,
        status: str | None = "active",
    ) -> list[sqlite3.Row]:
        return self.identity.list_manual_identity_annotations(
            session_id,
            diarization_run_id=diarization_run_id,
            status=status,
        )

    def retract_manual_identity_annotation(
        self, annotation_id: int
    ) -> sqlite3.Row:
        return self.identity.retract_manual_identity_annotation(annotation_id)

    def upsert_identity_candidate_review(
        self, run_id: int, values: dict[str, Any]
    ) -> sqlite3.Row:
        return self.identity.upsert_identity_candidate_review(run_id, values)

    def list_identity_candidate_reviews(self, run_id: int) -> list[sqlite3.Row]:
        return self.identity.list_identity_candidate_reviews(run_id)

    def upsert_identity_reference_interval(
        self, values: dict[str, Any]
    ) -> sqlite3.Row:
        return self.identity.upsert_identity_reference_interval(values)

    def list_identity_reference_intervals(
        self, identity_label: str | None = None
    ) -> list[sqlite3.Row]:
        return self.identity.list_identity_reference_intervals(identity_label)

    def list_processing_run_inputs(self, run_id: int) -> list[sqlite3.Row]:
        return self.runs.list_processing_run_inputs(run_id)

    def get_processing_run(self, run_id: int) -> sqlite3.Row:
        return self.runs.get_processing_run(run_id)

    def create_asr_hypothesis(
        self, values: dict[str, Any], tokens: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        return self.asr.create_asr_hypothesis(values, tokens)

    def get_asr_hypothesis(self, hypothesis_id: int) -> sqlite3.Row:
        return self.asr.get_asr_hypothesis(hypothesis_id)

    def list_asr_hypotheses(
        self, run_id: int, *, role: str | None = None
    ) -> list[sqlite3.Row]:
        return self.asr.list_asr_hypotheses(run_id, role=role)

    def list_asr_tokens(
        self, hypothesis_id: int, *, core_only: bool = False
    ) -> list[sqlite3.Row]:
        return self.asr.list_asr_tokens(hypothesis_id, core_only=core_only)

    def list_asr_token_sources(self, token_id: int) -> list[sqlite3.Row]:
        return self.asr.list_asr_token_sources(token_id)

    def list_asr_token_sources_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return self.asr.list_asr_token_sources_for_run(run_id)

    def list_committed_asr_tokens(self, run_id: int) -> list[sqlite3.Row]:
        return self.asr.list_committed_asr_tokens(run_id)

    def create_diarization_turns(
        self, run_id: int, session_id: int, turns: Sequence[dict[str, Any]]
    ) -> list[sqlite3.Row]:
        return self.diarization.create_diarization_turns(run_id, session_id, turns)

    def get_diarization_turn(self, turn_id: int) -> sqlite3.Row:
        return self.diarization.get_diarization_turn(turn_id)

    def list_diarization_turns(
        self, run_id: int, *, turn_kind: str | None = None
    ) -> list[sqlite3.Row]:
        return self.diarization.list_diarization_turns(run_id, turn_kind=turn_kind)

    def list_diarization_turn_sources(self, turn_id: int) -> list[sqlite3.Row]:
        return self.diarization.list_diarization_turn_sources(turn_id)

    def create_token_speaker_attributions(
        self,
        run_id: int,
        asr_run_id: int,
        attributions: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        return self.diarization.create_token_speaker_attributions(
            run_id,
            asr_run_id,
            attributions,
        )

    def list_token_speaker_attributions(
        self, run_id: int, *, token_id: int | None = None
    ) -> list[sqlite3.Row]:
        return self.diarization.list_token_speaker_attributions(
            run_id,
            token_id=token_id,
        )

    def create_asr_disagreement(self, values: dict[str, Any]) -> sqlite3.Row:
        return self.asr.create_asr_disagreement(values)

    def list_asr_disagreements(self, run_id: int) -> list[sqlite3.Row]:
        return self.asr.list_asr_disagreements(run_id)

    def segment_count(self, recording_id: int) -> int:
        return self.asr.segment_count(recording_id)

    def replace_vad_segments(
        self,
        recording_id: int,
        segments: Sequence[tuple[int, int]],
        source_path: str,
    ) -> None:
        return self.asr.replace_vad_segments(recording_id, segments, source_path)

    def reset_failed_segments(self, recording_id: int) -> int:
        return self.asr.reset_failed_segments(recording_id)

    def reset_interrupted_segments(self, recording_id: int) -> int:
        return self.asr.reset_interrupted_segments(recording_id)

    def reset_all_asr_segments(self, recording_id: int) -> int:
        return self.asr.reset_all_asr_segments(recording_id)

    def pending_segments(
        self, recording_id: int, limit: int | None = None
    ) -> list[sqlite3.Row]:
        return self.asr.pending_segments(recording_id, limit)

    def all_segments(self, recording_id: int, completed_only: bool = False) -> list[sqlite3.Row]:
        return self.asr.all_segments(recording_id, completed_only)

    def mark_segment_running(self, segment_id: int) -> None:
        return self.asr.mark_segment_running(segment_id)

    def mark_segment_completed(
        self,
        segment_id: int,
        *,
        language: str | None,
        text_raw: str,
        text_display: str,
        asr_model: str,
    ) -> None:
        return self.asr.mark_segment_completed(
            segment_id,
            language=language,
            text_raw=text_raw,
            text_display=text_display,
            asr_model=asr_model,
        )

    def mark_segment_failed(self, segment_id: int, error: str) -> None:
        return self.asr.mark_segment_failed(segment_id, error)

    def segment_status_counts(self, recording_id: int) -> dict[str, int]:
        return self.asr.segment_status_counts(recording_id)

    def replace_speaker_labels(
        self, recording_id: int, labels: Sequence[tuple[int, str | None]]
    ) -> None:
        return self.diarization.replace_speaker_labels(recording_id, labels)

    def get_segment(self, segment_id: int) -> sqlite3.Row:
        return self.identity.get_segment(segment_id)

    def upsert_self_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        return self.identity.upsert_self_profile(
            display_name=display_name,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            embedding_path=embedding_path,
        )

    def get_self_profile(self) -> sqlite3.Row | None:
        return self.identity.get_self_profile()

    def upsert_known_person_profile(
        self,
        *,
        display_name: str,
        embedding_model: str,
        embedding_version: str,
        embedding_path: str,
    ) -> sqlite3.Row:
        return self.identity.upsert_known_person_profile(
            display_name=display_name,
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            embedding_path=embedding_path,
        )

    def get_person_profile(self, person_id: int) -> sqlite3.Row:
        return self.identity.get_person_profile(person_id)

    def list_person_profiles(self) -> list[sqlite3.Row]:
        return self.identity.list_person_profiles()

    def clear_person_assignments(self, recording_id: int, person_id: int) -> int:
        return self.identity.clear_person_assignments(recording_id, person_id)

    def person_assignment_count(self, person_id: int) -> int:
        return self.identity.person_assignment_count(person_id)

    def delete_person_profile(self, person_id: int) -> bool:
        return self.identity.delete_person_profile(person_id)

    def assign_person_to_speaker(
        self,
        recording_id: int,
        speaker_session_id: str,
        person_id: int,
        score: float = 1.0,
    ) -> int:
        return self.identity.assign_person_to_speaker(
            recording_id,
            speaker_session_id,
            person_id,
            score,
        )

    def assign_person_to_segments(
        self,
        recording_id: int,
        person_id: int,
        segment_scores: Sequence[tuple[int, float]],
    ) -> int:
        return self.identity.assign_person_to_segments(
            recording_id,
            person_id,
            segment_scores,
        )

    def upsert_segment_annotations(
        self, recording_id: int, annotations: Sequence[dict[str, Any]]
    ) -> int:
        return self.identity.upsert_segment_annotations(recording_id, annotations)

    def list_segment_annotations(self, recording_id: int) -> list[sqlite3.Row]:
        return self.identity.list_segment_annotations(recording_id)

    def upsert_voice_library_sample(self, values: dict[str, Any]) -> sqlite3.Row:
        return self.identity.upsert_voice_library_sample(values)

    def list_voice_library_samples(
        self, *, person_id: int | None = None, recording_id: int | None = None
    ) -> list[sqlite3.Row]:
        return self.identity.list_voice_library_samples(
            person_id=person_id,
            recording_id=recording_id,
        )

    def replace_conversation_events(
        self,
        recording_id: int,
        events: Sequence[dict[str, Any]],
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM conversation_events WHERE recording_id = ?", (recording_id,)
            )
            connection.executemany(
                """
                INSERT INTO conversation_events (
                    recording_id, start_ms, end_ms, title, summary,
                    segment_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        recording_id,
                        event["start_ms"],
                        event["end_ms"],
                        event.get("title"),
                        event.get("summary"),
                        json.dumps(event["segment_ids"], ensure_ascii=False),
                        now,
                    )
                    for event in events
                ],
            )

    def list_conversation_events(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM conversation_events
                    WHERE recording_id = ? ORDER BY start_ms
                    """,
                    (recording_id,),
                )
            )

    def record_evaluation_run(
        self,
        recording_id: int,
        *,
        truth_path: str,
        truth_sha256: str,
        config: dict[str, Any],
        metrics: dict[str, Any],
        report_json_path: str,
        report_markdown_path: str,
    ) -> int:
        return self.evaluation.record_evaluation_run(
            recording_id,
            truth_path=truth_path,
            truth_sha256=truth_sha256,
            config=config,
            metrics=metrics,
            report_json_path=report_json_path,
            report_markdown_path=report_markdown_path,
        )

    def list_evaluation_runs(self, recording_id: int) -> list[sqlite3.Row]:
        return self.evaluation.list_evaluation_runs(recording_id)

    def create_truth_set(
        self, values: dict[str, Any], annotations: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        return self.evaluation.create_truth_set(values, annotations)

    def get_truth_set(self, truth_set_id: int) -> sqlite3.Row:
        return self.evaluation.get_truth_set(truth_set_id)

    def list_truth_sets(self, session_id: int | None = None) -> list[sqlite3.Row]:
        return self.evaluation.list_truth_sets(session_id)

    def list_truth_annotations(
        self, truth_set_id: int, *, annotation_kind: str | None = None
    ) -> list[sqlite3.Row]:
        return self.evaluation.list_truth_annotations(
            truth_set_id,
            annotation_kind=annotation_kind,
        )

    def list_truth_annotation_sources(self, annotation_id: int) -> list[sqlite3.Row]:
        return self.evaluation.list_truth_annotation_sources(annotation_id)

    def create_benchmark_prediction_set(
        self, values: dict[str, Any], predictions: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        return self.evaluation.create_benchmark_prediction_set(values, predictions)

    def get_benchmark_prediction_set(self, prediction_set_id: int) -> sqlite3.Row:
        return self.evaluation.get_benchmark_prediction_set(prediction_set_id)

    def list_benchmark_prediction_sets(
        self, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        return self.evaluation.list_benchmark_prediction_sets(session_id)

    def list_benchmark_predictions(
        self, prediction_set_id: int, *, prediction_kind: str | None = None
    ) -> list[sqlite3.Row]:
        return self.evaluation.list_benchmark_predictions(
            prediction_set_id,
            prediction_kind=prediction_kind,
        )

    def record_benchmark_run(
        self,
        truth_set_id: int,
        prediction_set_id: int,
        *,
        config: dict[str, Any],
        metrics: dict[str, Any],
        details: dict[str, Any],
        report_json_path: str,
        report_markdown_path: str,
    ) -> int:
        return self.evaluation.record_benchmark_run(
            truth_set_id,
            prediction_set_id,
            config=config,
            metrics=metrics,
            details=details,
            report_json_path=report_json_path,
            report_markdown_path=report_markdown_path,
        )

    def list_benchmark_runs(self, truth_set_id: int) -> list[sqlite3.Row]:
        return self.evaluation.list_benchmark_runs(truth_set_id)

    def upsert_action_candidate(self, values: dict[str, Any]) -> sqlite3.Row:
        return self.actions.upsert_action_candidate(values)

    def list_action_candidates(
        self, recording_id: int, *, status: str | None = None
    ) -> list[sqlite3.Row]:
        return self.actions.list_action_candidates(recording_id, status=status)

    def get_action_candidate(self, candidate_id: int) -> sqlite3.Row:
        return self.actions.get_action_candidate(candidate_id)

    def review_action_candidate(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        scheduled_at: str | None = None,
        location: str | None = None,
    ) -> sqlite3.Row:
        return self.actions.review_action_candidate(
            candidate_id,
            status=status,
            title=title,
            scheduled_at=scheduled_at,
            location=location,
        )
