from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from allday_asr.audio.tools import AudioToolError, extract_clip
from allday_asr.domain.hashing import canonical_json_sha256
from allday_asr.interfaces.web.audio import AudioRangeResponseMixin
from allday_asr.interfaces.web.responses import LocalResponseMixin
from allday_asr.v3.application.timeline_quality import run_timeline_quality_audit
from allday_asr.v3.domain.timeline_quality import TIMELINE_AUDIT_FORMAT


TEMP_LABEL_FORMAT = "AllDayRecording V3.1 temporary human labeling v1"
TEMP_LABEL_EXPORT_FORMAT = "AllDayRecording V3.1 human labels v1"
ASSET_ROOT = Path(__file__).parents[1] / "web_assets" / "temp_labeler"
IDENTITY_LABELS = {
    "self",
    "mother",
    "father",
    "other_live",
    "media",
    "mixed",
    "uncertain",
}
MIN_SELF_WINDOWS = 7
MIN_REVIEWED_SEAMS = 10
MIN_REFERENCE_UTTERANCES = 30
MIN_OVERLAP_UTTERANCES = 5
SEAM_FOCUS_MS = 5_000
SEAM_CONTEXT_MS = 7_000
NEXT_RECORDING_CANDIDATE_LIMIT = 40
PRIVATE_IDENTITY_FIELDS = {
    "audio_source_path",
    "audio_start_ms",
    "audio_end_ms",
    "speaker_track",
    "known_self_track",
}


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _write_json_atomically(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _open_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve(strict=True).as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(4 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


class TemporaryV31LabelApplication:
    """Loopback-only, disposable V3.1 labeling workspace."""

    def __init__(
        self,
        bundle_path: Path,
        database_path: Path,
        *,
        state_dir: Path,
    ) -> None:
        self.bundle_path = bundle_path.resolve(strict=True)
        self.database_path = database_path.resolve(strict=True)
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir = self.state_dir / "clips"
        self.state_path = self.state_dir / "progress.json"
        self.lock = threading.RLock()
        self.host = "127.0.0.1"
        self.port = 0
        self._bundle = _read_json(self.bundle_path)
        self._source_database_sha256 = _sha256_file(self.database_path)
        self._validate_bundle()
        self._ensure_state()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _validate_bundle(self) -> None:
        source = self._bundle.get("source")
        if not isinstance(source, dict):
            raise ValueError("迁移包缺少 source")
        expected = str(source.get("database_sha256") or "")
        if expected != self._source_database_sha256:
            raise ValueError("迁移包与当前旧数据库不匹配，已停止以免标错音频")
        assessment = self._bundle.get("assessment")
        if not isinstance(assessment, dict):
            raise ValueError("迁移包缺少 assessment")

    def _ensure_state(self) -> None:
        with self.lock:
            if self.state_path.is_file():
                state = _read_json(self.state_path)
                if state.get("format") != TEMP_LABEL_FORMAT:
                    raise ValueError("临时标注进度文件格式不兼容")
                if (
                    state.get("source", {}).get("database_sha256")
                    != self._source_database_sha256
                ):
                    raise ValueError("现有标注进度属于另一份数据库")
                if int(state.get("interaction_version") or 1) < 2:
                    self._upgrade_identity_queue(state)
                if int(state.get("interaction_version") or 1) < 3:
                    self._switch_to_next_recording(state)
                if int(state.get("interaction_version") or 1) < 4:
                    self._switch_to_confirmed_self_recording(state)
                return
            state = self._build_initial_state()
            _write_json_atomically(self.state_path, state)
            self._switch_to_next_recording(state)
            self._switch_to_confirmed_self_recording(state)

    def _build_initial_state(self) -> dict[str, Any]:
        assessment = self._bundle["assessment"]
        recommended = assessment["seam_quality"]["recommended_seam_ids"]
        if len(recommended) < MIN_REVIEWED_SEAMS:
            raise ValueError("迁移包没有提供足够的推荐接缝")
        session_id = int(str(recommended[0]).split(":")[1])
        connection = _open_read_only(self.database_path)
        try:
            source = connection.execute(
                """
                SELECT rs.session_key, rs.duration_ms, rs.legacy_recording_id,
                       r.source_path, r.normalized_path, r.sha256
                FROM recording_sessions rs
                JOIN recordings r ON r.id = rs.legacy_recording_id
                WHERE rs.id = ?
                """,
                (session_id,),
            ).fetchone()
            if source is None:
                raise ValueError(f"找不到接缝对应的旧录音 session {session_id}")
            source_path = Path(str(source["source_path"])).resolve(strict=True)
            session = {
                "legacy_session_id": session_id,
                "session_key": str(source["session_key"]),
                "duration_ms": int(source["duration_ms"]),
                "legacy_recording_id": int(source["legacy_recording_id"]),
                "source_path": str(source_path),
                "source_audio_sha256": str(source["sha256"]),
            }
            candidates = self._identity_candidates(connection, session, source)
            seams, predictions = self._seam_queue(connection, session, recommended)
        finally:
            connection.close()
        created_at = _now()
        return {
            "format": TEMP_LABEL_FORMAT,
            "interaction_version": 2,
            "created_at": created_at,
            "updated_at": created_at,
            "source": {
                "bundle_path": str(self.bundle_path),
                "bundle_sha256": str(self._bundle.get("bundle_sha256") or ""),
                "database_path": str(self.database_path),
                "database_sha256": self._source_database_sha256,
                "read_only": True,
            },
            "session": session,
            "targets": {
                "additional_self_windows": MIN_SELF_WINDOWS,
                "reviewed_seams": MIN_REVIEWED_SEAMS,
                "reference_utterances": MIN_REFERENCE_UTTERANCES,
                "overlap_utterances": MIN_OVERLAP_UTTERANCES,
            },
            "identity_candidates": candidates,
            "identity_annotations": {},
            "seams": seams,
            "original_predictions": predictions,
            "finalized": None,
        }

    def _identity_candidates(
        self,
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
        source: sqlite3.Row,
    ) -> list[dict[str, Any]]:
        normalized = Path(str(source["normalized_path"] or ""))
        score_path = normalized.parent / "self-candidates" / "scores.json"
        annotations_path = normalized.parent / "self-candidates" / "annotations.json"
        scored = _read_json(score_path).get("segments", []) if score_path.is_file() else []
        old_annotations: dict[int, dict[str, Any]] = {}
        if annotations_path.is_file():
            annotations = _read_json(annotations_path).get("annotations", [])
            old_annotations = {
                int(item["segment_id"]): item
                for item in annotations
                if isinstance(item, dict) and item.get("segment_id") is not None
            }
        if not scored:
            scored = [
                {
                    "segment_id": int(row["id"]),
                    "start_ms": int(row["start_ms"]),
                    "end_ms": int(row["end_ms"]),
                    "text": str(row["text_display"] or ""),
                    "score_median": None,
                }
                for row in connection.execute(
                    """
                    SELECT id, start_ms, end_ms, text_display
                    FROM speech_segments WHERE recording_id = ?
                    ORDER BY start_ms
                    """,
                    (int(session["legacy_recording_id"]),),
                )
            ]
        confirmed_self = {
            segment_id
            for segment_id, value in old_annotations.items()
            if value.get("identity_label") == "self"
        }
        eligible_scored = [
            value
            for value in scored
            if isinstance(value, dict)
            and (
                old_annotations.get(int(value.get("segment_id") or 0)) is None
                or old_annotations[int(value.get("segment_id") or 0)].get(
                    "identity_label"
                )
                == "self"
            )
        ]
        ordered = sorted(
            eligible_scored,
            key=lambda value: (
                int(value.get("segment_id") or 0) not in confirmed_self,
                -(float(value.get("score_median") or -2.0)),
                int(value.get("start_ms") or 0),
            ),
        )
        output: list[dict[str, Any]] = []
        selected_spans: list[tuple[int, int]] = []
        duration_ms = int(session["duration_ms"])
        for value in ordered:
            if len(output) >= 80:
                break
            segment_id = int(value["segment_id"])
            raw_start = int(value["start_ms"])
            raw_end = int(value["end_ms"])
            start_ms, end_ms = _identity_window(raw_start, raw_end, duration_ms)
            if any(start_ms < right + 500 and end_ms > left - 500 for left, right in selected_spans):
                continue
            old = old_annotations.get(segment_id)
            output.append(
                {
                    "candidate_id": f"legacy-session-{session['legacy_session_id']}-segment-{segment_id}",
                    "segment_id": segment_id,
                    "session_id": int(session["legacy_session_id"]),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "source_start_ms": raw_start,
                    "source_end_ms": raw_end,
                    "old_human_label": old.get("identity_label") if old else None,
                    "old_human_raw_label": old.get("raw_label") if old else None,
                    "old_segment_too_short": raw_end - raw_start < 2_000,
                    "selection_score": value.get("score_median"),
                    "text_hint": str(value.get("text") or ""),
                }
            )
            selected_spans.append((start_ms, end_ms))
        if len(output) < 20:
            raise ValueError("无法建立足够的本人候选队列")
        return output

    def _upgrade_identity_queue(self, state: dict[str, Any]) -> None:
        """Drop already-known non-self clips while preserving every saved answer."""

        connection = _open_read_only(self.database_path)
        try:
            source = connection.execute(
                """
                SELECT rs.session_key, rs.duration_ms, rs.legacy_recording_id,
                       r.source_path, r.normalized_path, r.sha256
                FROM recording_sessions rs
                JOIN recordings r ON r.id = rs.legacy_recording_id
                WHERE rs.id = ?
                """,
                (int(state["session"]["legacy_session_id"]),),
            ).fetchone()
            if source is None:
                raise ValueError("无法升级本人候选队列：旧录音不存在")
            rebuilt = self._identity_candidates(connection, state["session"], source)
        finally:
            connection.close()
        rebuilt_ids = {str(value["candidate_id"]) for value in rebuilt}
        annotations = set(state.get("identity_annotations", {}))
        preserved = [
            value
            for value in state.get("identity_candidates", [])
            if str(value.get("candidate_id")) in annotations
            and str(value.get("candidate_id")) not in rebuilt_ids
        ]
        state["identity_candidates"] = [*rebuilt, *preserved]
        state["interaction_version"] = 2
        state["updated_at"] = _now()
        _write_json_atomically(self.state_path, state)

    def _switch_to_next_recording(self, state: dict[str, Any]) -> None:
        """Put a fresh recording first without changing any saved answers."""

        connection = _open_read_only(self.database_path)
        try:
            candidates, source_summary = self._next_recording_identity_candidates(
                connection,
                excluded_session_id=int(state["session"]["legacy_session_id"]),
            )
        finally:
            connection.close()
        existing_ids = {
            str(value.get("candidate_id"))
            for value in state.get("identity_candidates", [])
        }
        fresh = [
            value
            for value in candidates
            if str(value.get("candidate_id")) not in existing_ids
        ]
        state["identity_candidates"] = [
            *fresh,
            *state.get("identity_candidates", []),
        ]
        state["active_identity_source"] = source_summary
        state["interaction_version"] = 3
        state["updated_at"] = _now()
        _write_json_atomically(self.state_path, state)

    def _next_recording_identity_candidates(
        self,
        connection: sqlite3.Connection,
        *,
        excluded_session_id: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        session = connection.execute(
            """
            SELECT rs.id, rs.session_key, rs.duration_ms
            FROM recording_sessions rs
            WHERE rs.id != ?
              AND EXISTS (
                  SELECT 1 FROM session_sources ss WHERE ss.session_id = rs.id
              )
              AND EXISTS (
                  SELECT 1 FROM processing_runs pr
                  WHERE pr.session_id = rs.id
                    AND pr.run_kind = 'quality_diarization_v2d'
                    AND pr.status = 'completed'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM manual_identity_annotations mia
                  WHERE mia.session_id = rs.id AND mia.status = 'active'
              )
            ORDER BY rs.id DESC
            LIMIT 1
            """,
            (excluded_session_id,),
        ).fetchone()
        if session is None:
            raise ValueError("没有找到另一份尚未标过人物的录音")
        session_id = int(session["id"])
        diarization_run = connection.execute(
            """
            SELECT id, config_json
            FROM processing_runs
            WHERE session_id = ?
              AND run_kind = 'quality_diarization_v2d'
              AND status = 'completed'
            ORDER BY id DESC
            LIMIT 1
            """,
            (session_id,),
        ).fetchone()
        if diarization_run is None:
            raise ValueError("下一份录音还没有可试听的人声片段")
        config = json.loads(str(diarization_run["config_json"]))
        asr_run_id = int(config["asr_run_id"])
        tokens = list(
            connection.execute(
                """
                SELECT token.text, token.session_start_ms, token.session_end_ms
                FROM asr_alignment_tokens token
                JOIN asr_hypotheses hypothesis
                  ON hypothesis.id = token.hypothesis_id
                WHERE hypothesis.run_id = ?
                  AND hypothesis.hypothesis_role = 'primary'
                  AND token.kept_in_core = 1
                ORDER BY token.session_start_ms, token.id
                """,
                (asr_run_id,),
            )
        )
        turns = list(
            connection.execute(
                """
                SELECT turn.id AS turn_id, turn.speaker_label,
                       turn.session_start_ms, turn.session_end_ms,
                       source.session_start_ms AS chunk_session_start_ms,
                       source.source_start_ms AS chunk_source_start_ms,
                       object.sha256 AS source_audio_sha256,
                       object.original_filename,
                       instance.id AS source_instance_id,
                       instance.source_path AS audio_source_path
                FROM diarization_turns turn
                JOIN session_sources source
                  ON source.session_id = turn.session_id
                 AND turn.session_start_ms >= source.session_start_ms
                 AND turn.session_end_ms <= source.session_end_ms
                JOIN source_objects object ON object.id = source.source_object_id
                JOIN source_instances instance ON instance.id = source.source_instance_id
                WHERE turn.run_id = ?
                  AND turn.turn_kind = 'exclusive'
                  AND turn.session_end_ms - turn.session_start_ms BETWEEN 2000 AND 5000
                ORDER BY turn.session_start_ms, turn.id
                """,
                (int(diarization_run["id"]),),
            )
        )
        if not turns:
            raise ValueError("下一份录音没有 2 到 5 秒的干净人声")

        prepared: list[dict[str, Any]] = []
        for turn in turns:
            start_ms = int(turn["session_start_ms"])
            end_ms = int(turn["session_end_ms"])
            text_hint = "".join(
                str(token["text"])
                for token in tokens
                if int(token["session_start_ms"]) < end_ms
                and int(token["session_end_ms"]) > start_ms
            ).strip()
            audio_start_ms = int(turn["chunk_source_start_ms"]) + (
                start_ms - int(turn["chunk_session_start_ms"])
            )
            prepared.append(
                {
                    "candidate_id": f"session-{session_id}-turn-{turn['turn_id']}",
                    "segment_id": None,
                    "session_id": session_id,
                    "session_key": str(session["session_key"]),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "source_start_ms": start_ms,
                    "source_end_ms": end_ms,
                    "old_human_label": None,
                    "old_human_raw_label": None,
                    "old_segment_too_short": False,
                    "selection_score": None,
                    "text_hint": text_hint,
                    "source_label": "下一份录音 · 约 "
                    f"{max(1, round(int(session['duration_ms']) / 60_000))} 分钟",
                    "source_file": str(turn["original_filename"]),
                    "source_audio_sha256": str(turn["source_audio_sha256"]),
                    "source_instance_id": int(turn["source_instance_id"]),
                    "audio_source_path": str(
                        Path(str(turn["audio_source_path"])).resolve(strict=True)
                    ),
                    "audio_start_ms": audio_start_ms,
                    "audio_end_ms": audio_start_ms + (end_ms - start_ms),
                    "speaker_track": str(turn["speaker_label"]),
                }
            )

        # Use same-session tracks only to shorten the review queue. Every clip
        # still requires a human answer before it becomes a label.
        speaker_first_person: dict[str, int] = {}
        for value in prepared:
            speaker = str(value["speaker_track"])
            speaker_first_person[speaker] = speaker_first_person.get(speaker, 0) + str(
                value["text_hint"]
            ).count("我")
        speaker_order = {
            speaker: index
            for index, speaker in enumerate(
                sorted(
                    speaker_first_person,
                    key=lambda value: (-speaker_first_person[value], value),
                )
            )
        }
        prepared.sort(
            key=lambda value: (
                speaker_order[str(value["speaker_track"])],
                -str(value["text_hint"]).count("我"),
                abs((int(value["end_ms"]) - int(value["start_ms"])) - 3_000),
                int(value["start_ms"]),
            )
        )
        source_summary = {
            "session_id": session_id,
            "session_key": str(session["session_key"]),
            "duration_ms": int(session["duration_ms"]),
            "candidate_count": min(len(prepared), NEXT_RECORDING_CANDIDATE_LIMIT),
        }
        return prepared[:NEXT_RECORDING_CANDIDATE_LIMIT], source_summary

    def _switch_to_confirmed_self_recording(self, state: dict[str, Any]) -> None:
        """Use unused windows from a recording already known to contain self."""

        connection = _open_read_only(self.database_path)
        try:
            candidates, source_summary = self._confirmed_self_recording_candidates(
                connection
            )
        finally:
            connection.close()
        existing_ids = {
            str(value.get("candidate_id"))
            for value in state.get("identity_candidates", [])
        }
        fresh = [
            value
            for value in candidates
            if str(value.get("candidate_id")) not in existing_ids
        ]
        state["identity_candidates"] = [
            *fresh,
            *state.get("identity_candidates", []),
        ]
        state["active_identity_source"] = source_summary
        state["interaction_version"] = 4
        state["updated_at"] = _now()
        _write_json_atomically(self.state_path, state)

    def _confirmed_self_recording_candidates(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        known = connection.execute(
            """
            SELECT annotation.session_id, annotation.diarization_run_id,
                   annotation.anonymous_speaker_label, session.session_key,
                   session.duration_ms, COUNT(*) AS confirmed_self
            FROM manual_identity_annotations annotation
            JOIN recording_sessions session ON session.id = annotation.session_id
            WHERE annotation.status = 'active'
              AND annotation.identity_label = 'self'
            GROUP BY annotation.session_id, annotation.diarization_run_id,
                     annotation.anonymous_speaker_label
            ORDER BY confirmed_self DESC, annotation.session_id DESC
            LIMIT 1
            """
        ).fetchone()
        if known is None:
            raise ValueError("没有找到另一份已确认含有本人声音的录音")
        session_id = int(known["session_id"])
        run_id = int(known["diarization_run_id"])
        run = connection.execute(
            "SELECT config_json FROM processing_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if run is None:
            raise ValueError("已确认录音的人声结果不存在")
        asr_run_id = int(json.loads(str(run["config_json"]))["asr_run_id"])
        tokens = list(
            connection.execute(
                """
                SELECT token.text, token.session_start_ms, token.session_end_ms
                FROM asr_alignment_tokens token
                JOIN asr_hypotheses hypothesis
                  ON hypothesis.id = token.hypothesis_id
                WHERE hypothesis.run_id = ?
                  AND hypothesis.hypothesis_role = 'primary'
                  AND token.kept_in_core = 1
                ORDER BY token.session_start_ms, token.id
                """,
                (asr_run_id,),
            )
        )
        turns = list(
            connection.execute(
                """
                SELECT turn.id AS turn_id, turn.session_start_ms,
                       turn.session_end_ms, source.session_start_ms AS chunk_start_ms,
                       source.source_start_ms AS chunk_source_start_ms,
                       object.sha256 AS source_audio_sha256,
                       object.original_filename, instance.id AS source_instance_id,
                       instance.source_path AS audio_source_path
                FROM diarization_turns turn
                JOIN session_sources source
                  ON source.session_id = turn.session_id
                JOIN source_objects object ON object.id = source.source_object_id
                JOIN source_instances instance ON instance.id = source.source_instance_id
                WHERE turn.run_id = ?
                  AND turn.turn_kind = 'exclusive'
                  AND turn.speaker_label = ?
                  AND CAST(
                      (turn.session_start_ms + turn.session_end_ms) / 2 AS INTEGER
                  ) - 1500 >= source.session_start_ms
                  AND CAST(
                      (turn.session_start_ms + turn.session_end_ms) / 2 AS INTEGER
                  ) + 1500 <= source.session_end_ms
                  AND NOT EXISTS (
                      SELECT 1 FROM manual_identity_annotations old
                      WHERE old.session_id = turn.session_id
                        AND old.status = 'active'
                        AND CAST(
                            (turn.session_start_ms + turn.session_end_ms) / 2
                            AS INTEGER
                        ) - 1500 < old.session_end_ms + 500
                        AND CAST(
                            (turn.session_start_ms + turn.session_end_ms) / 2
                            AS INTEGER
                        ) + 1500 > old.session_start_ms - 500
                  )
                ORDER BY turn.session_start_ms, turn.id
                """,
                (run_id, str(known["anonymous_speaker_label"])),
            )
        )
        prepared: list[dict[str, Any]] = []
        for turn in turns:
            center = (int(turn["session_start_ms"]) + int(turn["session_end_ms"])) // 2
            start_ms = center - 1_500
            end_ms = center + 1_500
            text_hint = "".join(
                str(token["text"])
                for token in tokens
                if int(token["session_start_ms"]) < end_ms
                and int(token["session_end_ms"]) > start_ms
            ).strip()
            audio_start_ms = int(turn["chunk_source_start_ms"]) + (
                start_ms - int(turn["chunk_start_ms"])
            )
            prepared.append(
                {
                    "candidate_id": f"session-{session_id}-turn-{turn['turn_id']}-context",
                    "segment_id": None,
                    "session_id": session_id,
                    "session_key": str(known["session_key"]),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "source_start_ms": int(turn["session_start_ms"]),
                    "source_end_ms": int(turn["session_end_ms"]),
                    "old_human_label": None,
                    "old_human_raw_label": None,
                    "old_segment_too_short": False,
                    "selection_score": None,
                    "text_hint": text_hint,
                    "source_label": "另一份录音 · 已确认含有你的声音 · 约 "
                    f"{max(1, round(int(known['duration_ms']) / 60_000))} 分钟",
                    "source_file": str(turn["original_filename"]),
                    "source_audio_sha256": str(turn["source_audio_sha256"]),
                    "source_instance_id": int(turn["source_instance_id"]),
                    "audio_source_path": str(
                        Path(str(turn["audio_source_path"])).resolve(strict=True)
                    ),
                    "audio_start_ms": audio_start_ms,
                    "audio_end_ms": audio_start_ms + 3_000,
                    "speaker_track": str(known["anonymous_speaker_label"]),
                    "known_self_track": True,
                }
            )
        if not prepared:
            raise ValueError("已确认录音里没有新的、不重叠的 3 秒片段")
        prepared.sort(
            key=lambda value: (
                -(int(value["source_end_ms"]) - int(value["source_start_ms"])),
                -str(value["text_hint"]).count("我"),
                int(value["start_ms"]),
            )
        )
        limit = min(12, len(prepared))
        return prepared[:limit], {
            "session_id": session_id,
            "session_key": str(known["session_key"]),
            "duration_ms": int(known["duration_ms"]),
            "candidate_count": limit,
            "confirmed_self_track": str(known["anonymous_speaker_label"]),
        }

    def _seam_queue(
        self,
        connection: sqlite3.Connection,
        session: Mapping[str, Any],
        recommended: list[Any],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        candidate_map = {
            str(value["seam_id"]): value
            for value in self._bundle.get("seam_candidates", [])
            if isinstance(value, dict) and value.get("seam_id")
        }
        human_refs = [
            value
            for value in self._bundle.get("timeline_reference_candidates", [])
            if isinstance(value, dict)
            and int(value.get("session_id") or -1) == int(session["legacy_session_id"])
        ]
        seams: list[dict[str, Any]] = []
        predictions: list[dict[str, Any]] = []
        recording_id = int(session["legacy_recording_id"])
        for raw_id in recommended[:MIN_REVIEWED_SEAMS]:
            seam_id = str(raw_id)
            candidate = candidate_map.get(seam_id)
            if candidate is None:
                raise ValueError(f"迁移包缺少接缝：{seam_id}")
            offset = int(candidate["offset_ms"])
            focus_start = offset - SEAM_FOCUS_MS
            focus_end = offset + SEAM_FOCUS_MS
            model_rows = list(
                connection.execute(
                    """
                    SELECT id, start_ms, end_ms, text_display, speaker_session_id,
                           person_id
                    FROM speech_segments
                    WHERE recording_id = ? AND start_ms < ? AND end_ms > ?
                    ORDER BY start_ms, id
                    """,
                    (recording_id, focus_end, focus_start),
                )
            )
            prediction_rows = [
                self._prediction_row(row, seam_id, offset, candidate)
                for row in model_rows
                if str(row["text_display"] or "").strip()
            ]
            predictions.extend(prediction_rows)
            drafts = self._human_prefill(human_refs, seam_id, offset)
            for prediction in prediction_rows:
                if any(
                    _spans_overlap(
                        int(prediction["start_ms"]),
                        int(prediction["end_ms"]),
                        int(existing["start_ms"]),
                        int(existing["end_ms"]),
                    )
                    for existing in drafts
                ):
                    continue
                drafts.append(
                    {
                        "utterance_id": f"draft-{prediction['utterance_id']}",
                        "start_ms": max(focus_start, int(prediction["start_ms"])),
                        "end_ms": min(focus_end, int(prediction["end_ms"])),
                        "text": str(prediction["text"]),
                        "speaker_id": str(prediction["speaker_id"]),
                        "has_overlap": False,
                        "prefill_source": "legacy_model_draft",
                    }
                )
            drafts.sort(key=lambda value: (int(value["start_ms"]), str(value["utterance_id"])))
            seams.append(
                {
                    "seam_id": seam_id,
                    "offset_ms": offset,
                    "left_chunk_id": str(candidate["left_chunk_id"]),
                    "right_chunk_id": str(candidate["right_chunk_id"]),
                    "clip_start_ms": max(0, offset - SEAM_CONTEXT_MS),
                    "clip_end_ms": min(int(session["duration_ms"]), offset + SEAM_CONTEXT_MS),
                    "reviewed": False,
                    "utterances": drafts,
                    "notes": "",
                }
            )
        return seams, predictions

    @staticmethod
    def _prediction_row(
        row: sqlite3.Row,
        seam_id: str,
        offset: int,
        candidate: Mapping[str, Any],
    ) -> dict[str, Any]:
        start_ms = int(row["start_ms"])
        end_ms = int(row["end_ms"])
        chunks: list[str] = []
        if start_ms < offset:
            chunks.append(str(candidate["left_chunk_id"]))
        if end_ms >= offset:
            chunks.append(str(candidate["right_chunk_id"]))
        return {
            "utterance_id": f"legacy-prediction-{row['id']}-{seam_id.rsplit(':', 1)[-1]}",
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": str(row["text_display"] or "").strip(),
            "speaker_id": (
                "self"
                if row["person_id"] == 2
                else str(row["speaker_session_id"] or "speaker-unknown")
            ),
            "source_chunk_ids": chunks,
            "seam_id": seam_id,
        }

    @staticmethod
    def _human_prefill(
        human_refs: list[dict[str, Any]], seam_id: str, offset: int
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for value in human_refs:
            start_ms = int(value["start_ms"])
            end_ms = int(value["end_ms"])
            if not _spans_overlap(start_ms, end_ms, offset - SEAM_FOCUS_MS, offset + SEAM_FOCUS_MS):
                continue
            if end_ms - start_ms > 10_000:
                continue
            speakers = value.get("speakers")
            speaker = (
                str(speakers[0])
                if isinstance(speakers, list) and speakers
                else "speaker-unknown"
            )
            output.append(
                {
                    "utterance_id": f"legacy-human-{value.get('truth_set_id')}-{start_ms}",
                    "start_ms": max(offset - SEAM_FOCUS_MS, start_ms),
                    "end_ms": min(offset + SEAM_FOCUS_MS, end_ms),
                    "text": str(value.get("text") or "").strip(),
                    "speaker_id": speaker,
                    "has_overlap": value.get("has_overlap") is True,
                    "prefill_source": "legacy_human_label",
                }
            )
        return output

    def task_payload(self) -> dict[str, Any]:
        with self.lock:
            state = _read_json(self.state_path)
        annotations = state["identity_annotations"]
        candidates = []
        for candidate in state["identity_candidates"]:
            candidate_id = str(candidate["candidate_id"])
            public_candidate = {
                key: value
                for key, value in candidate.items()
                if key not in PRIVATE_IDENTITY_FIELDS
            }
            candidates.append(
                {
                    **public_candidate,
                    "audio_url": f"/audio/identity/{candidate_id}",
                    "annotation": annotations.get(candidate_id),
                }
            )
        seams = []
        for seam in state["seams"]:
            offset = int(seam["offset_ms"])
            seams.append(
                {
                    **seam,
                    "audio_url": f"/audio/seam/{seam['seam_id']}",
                    "seam_at_seconds": (offset - int(seam["clip_start_ms"])) / 1000,
                    "utterances": [
                        {
                            **utterance,
                            "start_offset_ms": int(utterance["start_ms"]) - offset,
                            "end_offset_ms": int(utterance["end_ms"]) - offset,
                        }
                        for utterance in seam["utterances"]
                    ],
                }
            )
        return {
            "format": state["format"],
            "updated_at": state["updated_at"],
            "targets": state["targets"],
            "progress": self._progress(state),
            "identity_candidates": candidates,
            "seams": seams,
            "finalized": state["finalized"],
        }

    def save_identity(self, candidate_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        label = str(payload.get("label") or "").strip()
        if label not in IDENTITY_LABELS:
            raise ValueError("身份选项无效")
        note = str(payload.get("note") or "").strip()
        if len(note) > 1_000:
            raise ValueError("备注不能超过 1000 字")
        with self.lock:
            state = _read_json(self.state_path)
            candidate = _find_by_id(state["identity_candidates"], "candidate_id", candidate_id)
            duration = int(candidate["end_ms"]) - int(candidate["start_ms"])
            if label == "self" and not 2_000 <= duration <= 5_000:
                raise ValueError("本人窗口必须是 2 到 5 秒")
            state["identity_annotations"][candidate_id] = {
                "label": label,
                "note": note,
                "reviewed_at": _now(),
                "start_ms": int(candidate["start_ms"]),
                "end_ms": int(candidate["end_ms"]),
            }
            self._touch(state)
            _write_json_atomically(self.state_path, state)
            return self._progress(state)

    def save_seam(self, seam_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload.get("utterances"), list):
            raise ValueError("utterances 必须是数组")
        if len(payload["utterances"]) > 100:
            raise ValueError("单个接缝最多 100 条语音")
        reviewed = payload.get("reviewed") is True
        notes = str(payload.get("notes") or "").strip()
        if len(notes) > 2_000:
            raise ValueError("接缝备注不能超过 2000 字")
        with self.lock:
            state = _read_json(self.state_path)
            seam = _find_by_id(state["seams"], "seam_id", seam_id)
            offset = int(seam["offset_ms"])
            utterances: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index, raw in enumerate(payload["utterances"]):
                if not isinstance(raw, dict):
                    raise ValueError("语音行必须是对象")
                utterance_id = str(raw.get("utterance_id") or f"human-{seam_id}-{index}").strip()
                if not utterance_id or utterance_id in seen:
                    raise ValueError("语音行 ID 为空或重复")
                seen.add(utterance_id)
                start_offset = _integer(raw.get("start_offset_ms"), "开始时间")
                end_offset = _integer(raw.get("end_offset_ms"), "结束时间")
                if not -SEAM_FOCUS_MS <= start_offset < end_offset <= SEAM_FOCUS_MS:
                    raise ValueError("语音时间必须位于接缝前后 5 秒内")
                text = str(raw.get("text") or "").strip()
                speaker_id = str(raw.get("speaker_id") or "").strip()
                if not text:
                    raise ValueError("每条语音都要填写听到的文字")
                if not speaker_id:
                    raise ValueError("每条语音都要选择人物")
                if len(text) > 4_000 or len(speaker_id) > 100:
                    raise ValueError("语音文字或人物名称过长")
                utterances.append(
                    {
                        "utterance_id": utterance_id,
                        "start_ms": offset + start_offset,
                        "end_ms": offset + end_offset,
                        "text": text,
                        "speaker_id": speaker_id,
                        "has_overlap": raw.get("has_overlap") is True,
                        "prefill_source": str(raw.get("prefill_source") or "human_added"),
                    }
                )
            seam["utterances"] = utterances
            seam["reviewed"] = reviewed
            seam["notes"] = notes
            seam["reviewed_at"] = _now() if reviewed else None
            self._touch(state)
            _write_json_atomically(self.state_path, state)
            return self._progress(state)

    def finalize(self) -> dict[str, Any]:
        with self.lock:
            state = _read_json(self.state_path)
            progress = self._progress(state)
            if not progress["ready"]:
                raise ValueError("还没标够：请先把页面顶部四项进度全部变绿")
            body = self._export_body(state)
            export_sha256 = canonical_json_sha256(body)
            export = {**body, "export_sha256": export_sha256}
            export_path = self.state_dir / f"v31-human-labels-{export_sha256[:12]}.json"
            _write_json_atomically(export_path, export)
            audit_path = self.state_dir / f"v31-seam-audit-{export_sha256[:12]}.json"
            _write_json_atomically(audit_path, self._audit_document(state))
            audit = run_timeline_quality_audit(audit_path)
            receipt_body = {
                "format": TEMP_LABEL_EXPORT_FORMAT,
                "receipt_version": 1,
                "source_database_sha256": self._source_database_sha256,
                "export_sha256": export_sha256,
                "progress": progress,
                "seam_audit": {
                    "accepted": audit.accepted,
                    "blockers": list(audit.blockers),
                    "input_sha256": audit.input_sha256,
                    "receipt_sha256": audit.receipt_sha256,
                },
            }
            receipt_sha256 = canonical_json_sha256(receipt_body)
            receipt_path = self.state_dir / f"v31-human-labels-{export_sha256[:12]}.receipt.json"
            _write_json_atomically(
                receipt_path,
                {**receipt_body, "receipt_sha256": receipt_sha256},
            )
            state["finalized"] = {
                "at": _now(),
                "export": str(export_path),
                "export_sha256": export_sha256,
                "receipt": str(receipt_path),
                "receipt_sha256": receipt_sha256,
                "seam_audit": str(audit_path),
                "seam_audit_receipt": str(audit.receipt_path),
                "seam_audit_accepted": audit.accepted,
                "seam_audit_blockers": list(audit.blockers),
            }
            state["updated_at"] = _now()
            _write_json_atomically(self.state_path, state)
            return state["finalized"]

    def audio_path(self, kind: str, item_id: str) -> Path:
        with self.lock:
            state = _read_json(self.state_path)
            if kind == "identity":
                item = _find_by_id(state["identity_candidates"], "candidate_id", item_id)
                start_ms = int(item["start_ms"])
                end_ms = int(item["end_ms"])
                source_path = Path(
                    str(item.get("audio_source_path") or state["session"]["source_path"])
                )
                source_start_ms = int(item.get("audio_start_ms", start_ms))
                source_end_ms = int(item.get("audio_end_ms", end_ms))
            elif kind == "seam":
                item = _find_by_id(state["seams"], "seam_id", item_id)
                start_ms = int(item["clip_start_ms"])
                end_ms = int(item["clip_end_ms"])
                source_path = Path(state["session"]["source_path"])
                source_start_ms = start_ms
                source_end_ms = end_ms
            else:
                raise KeyError(kind)
            safe = hashlib.sha256(f"{kind}:{item_id}:{start_ms}:{end_ms}".encode()).hexdigest()[:20]
            destination = self.clips_dir / f"{kind}-{safe}.wav"
            if destination.is_file() and destination.stat().st_size > 44:
                return destination
            extract_clip(
                source_path,
                destination,
                source_start_ms,
                source_end_ms,
            )
            return destination

    @staticmethod
    def _touch(state: dict[str, Any]) -> None:
        state["updated_at"] = _now()
        state["finalized"] = None

    @staticmethod
    def _progress(state: Mapping[str, Any]) -> dict[str, Any]:
        annotations = state["identity_annotations"]
        self_count = sum(value.get("label") == "self" for value in annotations.values())
        reviewed = [value for value in state["seams"] if value.get("reviewed") is True]
        references = sum(len(value.get("utterances", [])) for value in reviewed)
        overlaps = sum(
            utterance.get("has_overlap") is True
            for seam in reviewed
            for utterance in seam.get("utterances", [])
        )
        progress = {
            "identity_reviewed": len(annotations),
            "self_windows": self_count,
            "self_remaining": max(0, MIN_SELF_WINDOWS - self_count),
            "reviewed_seams": len(reviewed),
            "reference_utterances": references,
            "overlap_utterances": overlaps,
        }
        progress["ready"] = (
            self_count >= MIN_SELF_WINDOWS
            and len(reviewed) >= MIN_REVIEWED_SEAMS
            and references >= MIN_REFERENCE_UTTERANCES
            and overlaps >= MIN_OVERLAP_UTTERANCES
        )
        return progress

    def _export_body(self, state: Mapping[str, Any]) -> dict[str, Any]:
        candidates = {
            value["candidate_id"]: value for value in state["identity_candidates"]
        }
        identity_windows = []
        for candidate_id, annotation in sorted(state["identity_annotations"].items()):
            candidate = candidates[candidate_id]
            identity_windows.append(
                {
                    "candidate_id": candidate_id,
                    "session_id": str(
                        candidate.get("session_key") or state["session"]["session_key"]
                    ),
                    "legacy_session_id": int(candidate["session_id"]),
                    "start_ms": int(annotation["start_ms"]),
                    "end_ms": int(annotation["end_ms"]),
                    "identity_label": str(annotation["label"]),
                    "human_reviewed": True,
                    "reviewed_at": str(annotation["reviewed_at"]),
                    "note": str(annotation.get("note") or ""),
                    "source_audio_sha256": str(
                        candidate.get("source_audio_sha256")
                        or state["session"]["source_audio_sha256"]
                    ),
                }
            )
        return {
            "format": TEMP_LABEL_EXPORT_FORMAT,
            "policy_version": "v3.1-labeling.1",
            "source": state["source"],
            "session": state["session"],
            "targets": state["targets"],
            "progress": self._progress(state),
            "identity_holdout_windows": identity_windows,
            "seams": [
                {
                    key: value[key]
                    for key in (
                        "seam_id",
                        "offset_ms",
                        "left_chunk_id",
                        "right_chunk_id",
                        "reviewed",
                        "reviewed_at",
                        "utterances",
                        "notes",
                    )
                }
                for value in state["seams"]
            ],
        }

    def _audit_document(self, state: Mapping[str, Any]) -> dict[str, Any]:
        predictions = [
            {key: value[key] for key in (
                "utterance_id",
                "start_ms",
                "end_ms",
                "text",
                "speaker_id",
                "source_chunk_ids",
            )}
            for value in state["original_predictions"]
        ]
        references = [
            {key: utterance[key] for key in (
                "utterance_id",
                "start_ms",
                "end_ms",
                "text",
                "speaker_id",
                "has_overlap",
            )}
            for seam in state["seams"]
            for utterance in seam["utterances"]
        ]
        return {
            "format": TIMELINE_AUDIT_FORMAT,
            "session_id": str(state["session"]["session_key"]),
            "truth_completeness": {
                "alignment": "exhaustive",
                "transcript": "exhaustive",
                "speaker": "exhaustive",
                "overlap": "exhaustive",
            },
            "seams": [
                {key: seam[key] for key in (
                    "seam_id",
                    "offset_ms",
                    "left_chunk_id",
                    "right_chunk_id",
                    "reviewed",
                )}
                for seam in state["seams"]
            ],
            "reference_utterances": references,
            "predicted_utterances": predictions,
            "metadata": {
                "created_by": "temporary-v31-label-web",
                "source_database_sha256": self._source_database_sha256,
                "prediction_source": "immutable-legacy-v2-output",
            },
        }


class TemporaryV31LabelRequestHandler(
    AudioRangeResponseMixin,
    LocalResponseMixin,
    BaseHTTPRequestHandler,
):
    asset_root = ASSET_ROOT
    server: "TemporaryV31LabelHTTPServer"

    @property
    def application(self) -> TemporaryV31LabelApplication:
        return self.server.application

    def do_GET(self) -> None:
        try:
            self._handle_get()
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (AudioToolError, OSError, ValueError) as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_POST(self) -> None:
        try:
            self._handle_post()
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except OSError as exc:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def _handle_get(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/":
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if path == "/assets/app.js":
            self._send_asset("app.js", "text/javascript; charset=utf-8")
            return
        if path == "/assets/styles.css":
            self._send_asset("styles.css", "text/css; charset=utf-8")
            return
        if path == "/api/task":
            self._send_json(HTTPStatus.OK, self.application.task_payload())
            return
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["audio", "identity"]:
            self._send_audio_range(
                self.application.audio_path("identity", "/".join(parts[2:]))
            )
            return
        if len(parts) >= 3 and parts[:2] == ["audio", "seam"]:
            self._send_audio_range(self.application.audio_path("seam", "/".join(parts[2:])))
            return
        raise KeyError(path)

    def _handle_post(self) -> None:
        path = unquote(urlparse(self.path).path)
        payload = self._read_json()
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[:2] == ["api", "identity"]:
            progress = self.application.save_identity("/".join(parts[2:]), payload)
            self._send_json(HTTPStatus.OK, {"ok": True, "progress": progress})
            return
        if len(parts) >= 3 and parts[:2] == ["api", "seams"]:
            progress = self.application.save_seam("/".join(parts[2:]), payload)
            self._send_json(HTTPStatus.OK, {"ok": True, "progress": progress})
            return
        if path == "/api/finalize":
            self._send_json(HTTPStatus.OK, self.application.finalize())
            return
        raise KeyError(path)

    def log_message(self, format: str, *args: Any) -> None:
        return


class TemporaryV31LabelHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        application: TemporaryV31LabelApplication,
    ) -> None:
        self.application = application
        super().__init__(address, TemporaryV31LabelRequestHandler)


def build_temporary_label_server(
    bundle_path: Path,
    database_path: Path,
    *,
    state_dir: Path,
    host: str = "127.0.0.1",
    port: int = 0,
) -> TemporaryV31LabelHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("临时标注页只能监听本机回环地址")
    application = TemporaryV31LabelApplication(
        bundle_path,
        database_path,
        state_dir=state_dir,
    )
    server = TemporaryV31LabelHTTPServer((host, port), application)
    application.host = host
    application.port = int(server.server_address[1])
    return server


def serve_temporary_labeler(
    bundle_path: Path,
    database_path: Path,
    *,
    state_dir: Path,
    host: str = "127.0.0.1",
    port: int = 8774,
    open_browser: bool = True,
) -> None:
    server = build_temporary_label_server(
        bundle_path,
        database_path,
        state_dir=state_dir,
        host=host,
        port=port,
    )
    print(f"V3.1 临时标注页：{server.application.base_url}")
    print(f"进度文件：{server.application.state_path}")
    if open_browser:
        webbrowser.open(server.application.base_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _identity_window(start_ms: int, end_ms: int, duration_ms: int) -> tuple[int, int]:
    raw_duration = end_ms - start_ms
    if raw_duration >= 5_000:
        center = (start_ms + end_ms) // 2
        start = center - 2_000
        end = center + 2_000
    elif raw_duration >= 2_000:
        start, end = start_ms, end_ms
    else:
        center = (start_ms + end_ms) // 2
        start = center - 1_500
        end = center + 1_500
    if start < 0:
        end -= start
        start = 0
    if end > duration_ms:
        start -= end - duration_ms
        end = duration_ms
    return max(0, start), min(duration_ms, end)


def _find_by_id(values: list[dict[str, Any]], key: str, selected: str) -> dict[str, Any]:
    for value in values:
        if str(value.get(key)) == selected:
            return value
    raise KeyError(selected)


def _integer(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label}必须是整数毫秒")
    return value


def _spans_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and left_end > right_start


__all__ = [
    "TEMP_LABEL_EXPORT_FORMAT",
    "TEMP_LABEL_FORMAT",
    "TemporaryV31LabelApplication",
    "TemporaryV31LabelHTTPServer",
    "build_temporary_label_server",
    "serve_temporary_labeler",
]
