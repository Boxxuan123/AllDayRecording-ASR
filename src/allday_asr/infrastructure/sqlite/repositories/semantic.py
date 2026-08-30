from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from .runs import RunRepository
from .sessions import SessionRepository
from .types import Clock, ConnectionFactory


class SemanticRepository:
    def __init__(
        self,
        connect: ConnectionFactory,
        *,
        runs: RunRepository,
        sessions: SessionRepository,
        now: Clock,
    ) -> None:
        self.connect = connect
        self._runs = runs
        self._sessions = sessions
        self._now = now

    def create_semantic_snapshot(
        self,
        run_id: int,
        exchange: dict[str, Any],
        candidates: Sequence[dict[str, Any]],
    ) -> list[sqlite3.Row]:
        """Append one private, immutable V2-E.0 exchange and its candidates."""
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "semantic_v2e0":
            raise ValueError("语义快照只适用于 V2-E.0 run")
        if str(run["status"]) != "running":
            raise ValueError("语义快照只能写入运行中的 V2-E.0 run")
        session_id = int(run["session_id"])
        asr_run_id = int(exchange["asr_run_id"])
        asr_run = self._runs.get_processing_run(asr_run_id)
        if (
            str(asr_run["run_kind"]) != "quality_asr_v2c"
            or str(asr_run["status"]) != "completed"
            or int(asr_run["session_id"]) != session_id
        ):
            raise ValueError("语义快照需要同会话已完成的 V2-C ASR run")
        diarization_run_id = exchange.get("diarization_run_id")
        if diarization_run_id is not None:
            diarization_run = self._runs.get_processing_run(int(diarization_run_id))
            if (
                str(diarization_run["run_kind"]) != "quality_diarization_v2d"
                or str(diarization_run["status"]) != "completed"
                or int(diarization_run["session_id"]) != session_id
            ):
                raise ValueError("语义快照的 V2-D run 无效")
        if exchange.get("audio_bytes_included") or exchange.get(
            "source_paths_included"
        ):
            raise ValueError("V2-E.0 禁止在语义交换中包含音频字节或原音路径")

        request_json = json.dumps(
            exchange["request"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        response_json = json.dumps(
            exchange["response"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        request_sha256 = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        response_sha256 = hashlib.sha256(response_json.encode("utf-8")).hexdigest()
        if exchange.get("request_sha256") not in {None, request_sha256}:
            raise ValueError("语义请求 SHA-256 不正确")
        if exchange.get("response_sha256") not in {None, response_sha256}:
            raise ValueError("语义响应 SHA-256 不正确")

        session = self._sessions.get_recording_session(session_id)
        duration_ms = int(session["duration_ms"])
        prepared: list[tuple[Any, ...]] = []
        for candidate in candidates:
            candidate_type = str(candidate["candidate_type"])
            if candidate_type not in {"daily_summary", "event", "fact", "action"}:
                raise ValueError("语义候选类型无效")
            start_ms = int(candidate["session_start_ms"])
            end_ms = int(candidate["session_end_ms"])
            if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
                raise ValueError("语义候选超出录音会话范围")
            confidence = candidate.get("confidence")
            if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
                raise ValueError("语义候选置信度无效")
            title = str(candidate["title"]).strip()
            body = str(candidate["body"]).strip()
            if not title or not body:
                raise ValueError("语义候选标题和内容不能为空")
            if len(title) > 500 or len(body) > 100_000:
                raise ValueError("语义候选内容过长")
            evidence_json = json.dumps(
                candidate.get("evidence", {}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            canonical_payload = {
                "candidate_key": str(candidate["candidate_key"]),
                "candidate_type": candidate_type,
                "session_start_ms": start_ms,
                "session_end_ms": end_ms,
                "title": title,
                "body": body,
                "confidence": confidence,
                "evidence": json.loads(evidence_json),
            }
            canonical = json.dumps(
                canonical_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            prepared.append(
                (
                    run_id,
                    str(candidate["candidate_key"]),
                    candidate_type,
                    start_ms,
                    end_ms,
                    title,
                    body,
                    float(confidence) if confidence is not None else None,
                    evidence_json,
                    content_sha256,
                    self._now(),
                )
            )

        with self.connect() as connection:
            now = self._now()
            connection.execute(
                """
                INSERT INTO semantic_exchanges (
                    run_id, session_id, asr_run_id, diarization_run_id,
                    provider, model, request_format, response_format,
                    request_json, request_sha256, response_json,
                    response_sha256, audio_bytes_included,
                    source_paths_included, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
                """,
                (
                    run_id,
                    session_id,
                    asr_run_id,
                    int(diarization_run_id)
                    if diarization_run_id is not None
                    else None,
                    str(exchange["provider"]),
                    str(exchange["model"]),
                    str(exchange["request_format"]),
                    str(exchange["response_format"]),
                    request_json,
                    request_sha256,
                    response_json,
                    response_sha256,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO semantic_candidates (
                    run_id, candidate_key, candidate_type,
                    session_start_ms, session_end_ms, title, body,
                    confidence, evidence_json, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
        return self.list_semantic_candidates(run_id)

    def get_semantic_exchange(self, run_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_exchanges WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"V2-E.0 run {run_id} 没有语义交换")
        return row

    def list_semantic_candidates(self, run_id: int) -> list[sqlite3.Row]:
        run = self._runs.get_processing_run(run_id)
        if str(run["run_kind"]) != "semantic_v2e0":
            raise ValueError("语义候选只适用于 V2-E.0 run")
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM semantic_candidates
                    WHERE run_id = ?
                    ORDER BY CASE candidate_type
                        WHEN 'daily_summary' THEN 0
                        WHEN 'event' THEN 1
                        WHEN 'fact' THEN 2
                        ELSE 3 END,
                        session_start_ms, id
                    """,
                    (run_id,),
                )
            )

    def get_semantic_candidate(self, candidate_id: int) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM semantic_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"语义候选 {candidate_id} 不存在")
        return row

    def create_semantic_candidate_revision(
        self,
        candidate_id: int,
        *,
        status: str,
        title: str | None = None,
        body: str | None = None,
        note: str | None = None,
    ) -> sqlite3.Row:
        if status not in {"confirmed", "rejected"}:
            raise ValueError("语义审核状态必须是 confirmed 或 rejected")
        candidate = self.get_semantic_candidate(candidate_id)
        run = self._runs.get_processing_run(int(candidate["run_id"]))
        if str(run["status"]) != "completed":
            raise ValueError("只能审核已完成 V2-E.0 run 的候选")
        with self.connect() as connection:
            latest = connection.execute(
                """
                SELECT * FROM semantic_candidate_revisions
                WHERE candidate_id = ? ORDER BY revision_index DESC LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            current_title = str(latest["title"] if latest else candidate["title"])
            current_body = str(latest["body"] if latest else candidate["body"])
            revised_title = str(title).strip() if title is not None else current_title
            revised_body = str(body).strip() if body is not None else current_body
            if not revised_title or not revised_body:
                raise ValueError("语义审核后的标题和内容不能为空")
            if len(revised_title) > 500 or len(revised_body) > 100_000:
                raise ValueError("语义审核内容过长")
            revision_index = int(latest["revision_index"] if latest else 0) + 1
            payload = {
                "candidate_id": candidate_id,
                "revision_index": revision_index,
                "status": status,
                "title": revised_title,
                "body": revised_body,
                "note": note.strip() if note and note.strip() else None,
            }
            canonical = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            cursor = connection.execute(
                """
                INSERT INTO semantic_candidate_revisions (
                    candidate_id, revision_index, status, title, body,
                    note, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    revision_index,
                    status,
                    revised_title,
                    revised_body,
                    payload["note"],
                    hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    self._now(),
                ),
            )
            revision_id = int(cursor.lastrowid)
            return connection.execute(
                "SELECT * FROM semantic_candidate_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()

    def list_semantic_candidate_revisions(
        self, candidate_id: int
    ) -> list[sqlite3.Row]:
        self.get_semantic_candidate(candidate_id)
        with self.connect() as connection:
            return list(
                connection.execute(
                    """
                    SELECT * FROM semantic_candidate_revisions
                    WHERE candidate_id = ? ORDER BY revision_index
                    """,
                    (candidate_id,),
                )
            )
