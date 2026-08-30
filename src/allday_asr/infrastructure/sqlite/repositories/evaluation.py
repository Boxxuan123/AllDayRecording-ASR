from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from .sessions import SessionRepository
from .types import Clock, ConnectionFactory


def _sha256_path(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(block_size):
            digest.update(block)
    return digest.hexdigest()


class EvaluationRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        sessions: SessionRepository,
        now: Clock,
        get_recording: Callable[[int], sqlite3.Row],
    ) -> None:
        self.connect = connect
        self._sessions = sessions
        self._now = now
        self._get_recording = get_recording

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
        self._get_recording(recording_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO evaluation_runs (
                    recording_id, truth_path, truth_sha256, config_json,
                    metrics_json, report_json_path, report_markdown_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    recording_id,
                    truth_path,
                    truth_sha256,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    report_json_path,
                    report_markdown_path,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_evaluation_runs(self, recording_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM evaluation_runs
                    WHERE recording_id = ? ORDER BY id
                    """,
                    (recording_id,),
                )
            )

    def create_truth_set(
        self, values: dict[str, Any], annotations: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        session = self._sessions.get_recording_session(session_id)
        expected_fingerprint = self._sessions.session_input_fingerprint(session_id)
        if str(values["input_fingerprint"]) != expected_fingerprint:
            raise ValueError("真值集的输入指纹与录音会话不一致")
        scope_start = int(values["scope_start_ms"])
        scope_end = int(values["scope_end_ms"])
        if scope_start < 0 or scope_end <= scope_start:
            raise ValueError("真值集时间范围无效")
        if scope_end > int(session["duration_ms"]):
            raise ValueError("真值集时间范围超过录音会话")
        truth_path = Path(str(values["truth_path"])).resolve(strict=True)
        actual_truth_sha256 = _sha256_path(truth_path)
        if str(values["truth_sha256"]) != actual_truth_sha256:
            raise ValueError("真值文件声明的 SHA-256 不正确")
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO truth_sets (
                    truth_key, name, session_id, parent_truth_set_id,
                    format_version, status, scope_start_ms, scope_end_ms,
                    input_fingerprint, completeness_json, truth_path,
                    truth_sha256, provenance_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["truth_key"],
                    values["name"],
                    values["session_id"],
                    values.get("parent_truth_set_id"),
                    values["format_version"],
                    values["scope_start_ms"],
                    values["scope_end_ms"],
                    values["input_fingerprint"],
                    json.dumps(
                        values.get("completeness", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    str(truth_path),
                    actual_truth_sha256,
                    json.dumps(
                        values.get("provenance", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    now,
                    now,
                ),
            )
            truth_set_id = int(cursor.lastrowid)
            for annotation in annotations:
                annotation_start = int(annotation["session_start_ms"])
                annotation_end = int(annotation["session_end_ms"])
                if (
                    annotation_start < scope_start
                    or annotation_end > scope_end
                    or annotation_end <= annotation_start
                ):
                    raise ValueError(
                        f"真值标注 {annotation['annotation_key']} 超出真值集时间范围"
                    )
                source_refs = list(annotation.get("source_refs", []))
                if not source_refs:
                    raise ValueError(
                        f"真值标注 {annotation['annotation_key']} 没有原始音频引用"
                    )
                annotation_cursor = connection.execute(
                    """
                    INSERT INTO truth_annotations (
                        truth_set_id, annotation_key, annotation_kind,
                        session_start_ms, session_end_ms, label, text,
                        metadata_json, legacy_segment_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        truth_set_id,
                        annotation["annotation_key"],
                        annotation["annotation_kind"],
                        annotation["session_start_ms"],
                        annotation["session_end_ms"],
                        annotation.get("label"),
                        annotation.get("text"),
                        json.dumps(
                            annotation.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        annotation.get("legacy_segment_id"),
                        now,
                    ),
                )
                annotation_id = int(annotation_cursor.lastrowid)
                mapped_cursor = annotation_start
                for position, source_ref in enumerate(source_refs):
                    source_instance_id = source_ref.get("source_instance_id")
                    source_matches = list(connection.execute(
                        """
                        SELECT so.sha256, ss.source_instance_id,
                               ss.session_start_ms, ss.session_end_ms,
                               ss.source_start_ms, ss.source_end_ms
                        FROM source_objects so
                        JOIN session_sources ss ON ss.source_object_id = so.id
                        WHERE so.id = ? AND ss.session_id = ?
                          AND (? IS NULL OR ss.source_instance_id = ?)
                        """,
                        (
                            source_ref["source_object_id"],
                            session_id,
                            source_instance_id,
                            source_instance_id,
                        ),
                    ))
                    if not source_matches:
                        raise KeyError(
                            f"原始音频对象 {source_ref['source_object_id']} 不属于当前会话"
                        )
                    if len(source_matches) > 1:
                        raise ValueError("真值源引用的原始音频实例不明确")
                    source = source_matches[0]
                    source_instance_id = int(source["source_instance_id"])
                    if str(source["sha256"]) != str(source_ref["source_sha256"]):
                        raise ValueError("真值源引用的 SHA-256 与不可变源对象不一致")
                    source_start = int(source_ref["source_start_ms"])
                    source_end = int(source_ref["source_end_ms"])
                    if (
                        source_start < int(source["source_start_ms"])
                        or source_end > int(source["source_end_ms"])
                        or source_end <= source_start
                    ):
                        raise ValueError("真值源引用超出原始对象在会话中的映射范围")
                    mapped_start = int(source["session_start_ms"]) + (
                        source_start - int(source["source_start_ms"])
                    )
                    mapped_end = mapped_start + (source_end - source_start)
                    if mapped_start != mapped_cursor or mapped_end > annotation_end:
                        raise ValueError("真值源引用没有连续覆盖标注时间范围")
                    mapped_cursor = mapped_end
                    connection.execute(
                        """
                        INSERT INTO truth_annotation_sources (
                            annotation_id, position, source_object_id, source_instance_id,
                            source_sha256, source_start_ms, source_end_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            annotation_id,
                            position,
                            source_ref["source_object_id"],
                            source_instance_id,
                            source_ref["source_sha256"],
                            source_start,
                            source_end,
                        ),
                    )
                if mapped_cursor != annotation_end:
                    raise ValueError("真值源引用没有完整覆盖标注时间范围")
            connection.execute(
                """
                UPDATE truth_sets
                SET status = 'frozen', frozen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, truth_set_id),
            )
        return self.get_truth_set(truth_set_id)

    def get_truth_set(self, truth_set_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM truth_sets WHERE id = ?", (truth_set_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"连续时间真值集 {truth_set_id} 不存在")
        return row

    def list_truth_sets(self, session_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(connection.execute("SELECT * FROM truth_sets ORDER BY id"))
            return list(
                connection.execute(
                    "SELECT * FROM truth_sets WHERE session_id = ? ORDER BY id",
                    (session_id,),
                )
            )

    def list_truth_annotations(
        self, truth_set_id: int, *, annotation_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_truth_set(truth_set_id)
        sql = "SELECT * FROM truth_annotations WHERE truth_set_id = ?"
        params: list[Any] = [truth_set_id]
        if annotation_kind is not None:
            sql += " AND annotation_kind = ?"
            params.append(annotation_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

    def list_truth_annotation_sources(self, annotation_id: int) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM truth_annotation_sources
                    WHERE annotation_id = ? ORDER BY position
                    """,
                    (annotation_id,),
                )
            )

    def create_benchmark_prediction_set(
        self, values: dict[str, Any], predictions: Sequence[dict[str, Any]]
    ) -> sqlite3.Row:
        session_id = int(values["session_id"])
        session = self._sessions.get_recording_session(session_id)
        expected_fingerprint = self._sessions.session_input_fingerprint(session_id)
        if str(values["input_fingerprint"]) != expected_fingerprint:
            raise ValueError("预测快照的输入指纹与录音会话不一致")
        canonical = json.dumps(
            list(predictions),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        actual_content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        supplied_content_sha256 = values.get("content_sha256")
        if (
            supplied_content_sha256 is not None
            and str(supplied_content_sha256) != actual_content_sha256
        ):
            raise ValueError("预测快照声明的内容 SHA-256 不正确")
        for prediction in predictions:
            start_ms = int(prediction["session_start_ms"])
            end_ms = int(prediction["session_end_ms"])
            if start_ms < 0 or end_ms <= start_ms or end_ms > int(session["duration_ms"]):
                raise ValueError(
                    f"预测 {prediction['prediction_key']} 超出录音会话时间范围"
                )
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_prediction_sets (
                    prediction_key, name, session_id, processing_run_id,
                    input_fingerprint, adapter, model_manifest_json,
                    content_sha256, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    values["prediction_key"],
                    values["name"],
                    values["session_id"],
                    values.get("processing_run_id"),
                    values["input_fingerprint"],
                    values["adapter"],
                    json.dumps(
                        values.get("model_manifest", {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    actual_content_sha256,
                    now,
                ),
            )
            prediction_set_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO benchmark_predictions (
                    prediction_set_id, prediction_key, prediction_kind,
                    session_start_ms, session_end_ms, label, text,
                    confidence, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        prediction_set_id,
                        prediction["prediction_key"],
                        prediction["prediction_kind"],
                        prediction["session_start_ms"],
                        prediction["session_end_ms"],
                        prediction.get("label"),
                        prediction.get("text"),
                        prediction.get("confidence"),
                        json.dumps(
                            prediction.get("metadata", {}),
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        now,
                    )
                    for prediction in predictions
                ],
            )
            connection.execute(
                """
                UPDATE benchmark_prediction_sets
                SET status = 'frozen', frozen_at = ?
                WHERE id = ?
                """,
                (now, prediction_set_id),
            )
        return self.get_benchmark_prediction_set(prediction_set_id)

    def get_benchmark_prediction_set(self, prediction_set_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM benchmark_prediction_sets WHERE id = ?",
                (prediction_set_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"预测快照 {prediction_set_id} 不存在")
        return row

    def list_benchmark_prediction_sets(
        self, session_id: int | None = None
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            if session_id is None:
                return list(
                    connection.execute(
                        "SELECT * FROM benchmark_prediction_sets ORDER BY id"
                    )
                )
            return list(
                connection.execute(
                    """
                    SELECT * FROM benchmark_prediction_sets
                    WHERE session_id = ? ORDER BY id
                    """,
                    (session_id,),
                )
            )

    def list_benchmark_predictions(
        self, prediction_set_id: int, *, prediction_kind: str | None = None
    ) -> list[sqlite3.Row]:
        self.get_benchmark_prediction_set(prediction_set_id)
        sql = "SELECT * FROM benchmark_predictions WHERE prediction_set_id = ?"
        params: list[Any] = [prediction_set_id]
        if prediction_kind is not None:
            sql += " AND prediction_kind = ?"
            params.append(prediction_kind)
        sql += " ORDER BY session_start_ms, session_end_ms, id"
        with self.connect() as connection:
            return list(connection.execute(sql, params))

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
        self.get_truth_set(truth_set_id)
        self.get_benchmark_prediction_set(prediction_set_id)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO benchmark_runs (
                    truth_set_id, prediction_set_id, config_json,
                    metrics_json, details_json, report_json_path,
                    report_markdown_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    truth_set_id,
                    prediction_set_id,
                    json.dumps(config, ensure_ascii=False, sort_keys=True),
                    json.dumps(metrics, ensure_ascii=False, sort_keys=True),
                    json.dumps(details, ensure_ascii=False, sort_keys=True),
                    report_json_path,
                    report_markdown_path,
                    self._now(),
                ),
            )
            return int(cursor.lastrowid)

    def list_benchmark_runs(self, truth_set_id: int) -> list[sqlite3.Row]:
        self.get_truth_set(truth_set_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT br.*, bps.name AS prediction_name,
                           bps.adapter AS prediction_adapter
                    FROM benchmark_runs br
                    JOIN benchmark_prediction_sets bps
                      ON bps.id = br.prediction_set_id
                    WHERE br.truth_set_id = ? ORDER BY br.id
                    """,
                    (truth_set_id,),
                )
            )
