from __future__ import annotations

import json
import secrets
import threading
import uuid
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from allday_asr.audio.tools import extract_clip
from allday_asr.config import load_config
from allday_asr.paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, recording_output_dir
from allday_asr.services.daily import run_daily
from allday_asr.services.evaluation import (
    evaluate_truth,
    evaluation_truth_path,
    list_evaluation_templates,
    load_evaluation_truth,
    update_evaluation_truth_segment,
)
from allday_asr.services.quality_diarization_v2d3 import (
    review_identity_candidate,
)
from allday_asr.services.semantic_v2e0 import review_semantic_candidate
from allday_asr.services.semantic_v2e02 import (
    run_semantic_v2e02,
    semantic_overview,
)
from allday_asr.services.sources import (
    LogicalWindow,
    logical_window_cache_key,
    materialize_logical_window,
    resolve_session_slices,
)
from allday_asr.services.speaker_timeline import (
    MAX_AUDIO_WINDOW_MS,
    speaker_timeline_overview,
    speaker_timeline_window,
)
from allday_asr.storage.database import Database

ASSET_ROOT = Path(__file__).parent / "web_assets"
MAX_JSON_BODY = 1024 * 1024


class WebApplication:
    def __init__(
        self,
        database_path: Path,
        config_path: Path,
        *,
        token: str | None = None,
    ):
        self.database_path = database_path.resolve()
        self.config_path = config_path.resolve()
        self.token = token or secrets.token_urlsafe(32)
        self.host = "127.0.0.1"
        self.port = 0
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jobs_lock = threading.Lock()
        self.truth_lock = threading.Lock()
        self.audio_lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def database(self) -> Database:
        return Database(self.database_path)

    def recordings(self) -> list[dict]:
        return [
            {
                "id": int(row["id"]),
                "status": row["status"],
                "duration_ms": int(row["duration_ms"]),
                "recorded_at": row["recorded_at"],
                "timezone": row["timezone"],
                "device": row["device"],
                "source_name": Path(row["source_path"]).name,
            }
            for row in self.database().list_recordings()
        ]

    def dashboard(self, recording_id: int) -> dict:
        database = self.database()
        recording = database.get_recording(recording_id)
        segment_counts = database.segment_status_counts(recording_id)
        annotations = database.list_segment_annotations(recording_id)
        events = database.list_conversation_events(recording_id)
        candidates = database.list_action_candidates(recording_id)
        runs = database.list_processing_runs(recording_id)
        templates = list_evaluation_templates(recording_id)
        stages = {
            row["stage"]: {
                "status": row["status"],
                "model_id": row["model_id"],
                "model_version": row["model_version"],
            }
            for row in database.list_stages(recording_id)
        }
        return {
            "recording": {
                "id": recording_id,
                "status": recording["status"],
                "duration_ms": int(recording["duration_ms"]),
                "recorded_at": recording["recorded_at"],
                "timezone": recording["timezone"],
                "device": recording["device"],
            },
            "segments": segment_counts,
            "annotations": len(annotations),
            "events": len(events),
            "actions": {
                "total": len(candidates),
                "pending": sum(row["status"] == "pending" for row in candidates),
                "confirmed": sum(row["status"] == "confirmed" for row in candidates),
            },
            "evaluations": templates,
            "runs": len(runs),
            "last_run": _processing_run_payload(runs[-1]) if runs else None,
            "stages": stages,
            "schema_version": database.schema_version(),
        }

    def evaluation(self, recording_id: int, name: str) -> dict:
        metadata, rows = load_evaluation_truth(recording_id, name)
        return {
            "metadata": metadata,
            "segments": [
                {
                    **row,
                    "audio_url": f"/api/audio/{int(row['segment_id'])}?v=3",
                    "context_audio_url": (
                        f"/api/audio/{int(row['segment_id'])}?mode=context&v=1"
                    ),
                }
                for row in rows
            ],
        }

    def update_evaluation_segment(
        self, recording_id: int, name: str, segment_id: int, values: dict
    ) -> dict:
        with self.truth_lock:
            return update_evaluation_truth_segment(
                recording_id, name, segment_id, values
            )

    def run_evaluation(self, recording_id: int, name: str) -> dict:
        summary = evaluate_truth(
            self.database(), evaluation_truth_path(recording_id, name)
        )
        return {
            "evaluation_run_id": summary.evaluation_run_id,
            "recording_id": summary.recording_id,
            "item_count": summary.item_count,
            "metrics": summary.metrics,
            "report_markdown_path": str(summary.report_markdown_path.resolve()),
            "report_json_path": str(summary.report_json_path.resolve()),
        }

    def actions(self, recording_id: int) -> list[dict]:
        return [_action_payload(row) for row in self.database().list_action_candidates(recording_id)]

    def review_action(self, candidate_id: int, values: dict) -> dict:
        allowed = {"status", "title", "scheduled_at", "location"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{', '.join(sorted(unknown))}")
        if "status" not in values:
            raise ValueError("缺少 status")
        row = self.database().review_action_candidate(
            candidate_id,
            status=str(values["status"]),
            title=values.get("title"),
            scheduled_at=values.get("scheduled_at"),
            location=values.get("location"),
        )
        return _action_payload(row)

    def runs(self, recording_id: int) -> list[dict]:
        return [
            _processing_run_payload(row)
            for row in reversed(self.database().list_processing_runs(recording_id))
        ]

    def speaker_timeline(
        self, recording_id: int, *, run_id: int | None = None
    ) -> dict:
        return speaker_timeline_overview(
            self.database(), recording_id, run_id=run_id
        )

    def speaker_timeline_window(
        self,
        recording_id: int,
        *,
        run_id: int,
        start_ms: int,
        end_ms: int,
    ) -> dict:
        return speaker_timeline_window(
            self.database(),
            recording_id,
            run_id=run_id,
            start_ms=start_ms,
            end_ms=end_ms,
        )

    def review_identity_expansion(
        self,
        recording_id: int,
        *,
        run_id: int,
        candidate_id: str,
        status: str,
        note: str | None = None,
    ) -> dict:
        database = self.database()
        run = database.get_processing_run(run_id)
        if int(run["recording_id"]) != recording_id:
            raise ValueError("V2-D.3 run 不属于当前录音")
        return review_identity_candidate(
            database,
            run_id,
            candidate_id=candidate_id,
            status=status,
            note=note,
        )

    def semantic(self, recording_id: int) -> dict[str, Any]:
        return semantic_overview(self.database(), recording_id)

    def generate_semantic(self, recording_id: int) -> dict[str, Any]:
        summary = run_semantic_v2e02(self.database(), recording_id)
        return {
            "run_id": summary.run_id,
            "recording_id": recording_id,
            "asr_run_id": summary.asr_run_id,
            "diarization_run_id": summary.diarization_run_id,
            "episode_count": summary.episode_count,
            "excluded_block_count": summary.excluded_block_count,
            "llm_job_count": summary.llm_job_count,
            "llm_payload_bytes": summary.llm_payload_bytes,
            "token_count": summary.token_count,
            "candidate_count": summary.candidate_count,
            "scene_count": summary.scene_count,
            "claim_count": summary.claim_count,
            "action_count": summary.action_count,
            "unresolved_count": summary.unresolved_count,
            "request_sha256": summary.request_sha256,
            "provider_request_sha256": summary.provider_request_sha256,
            "response_sha256": summary.response_sha256,
        }

    def review_semantic(
        self, recording_id: int, candidate_id: int, values: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {"recording_id", "status", "title", "body", "note"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"不允许更新字段：{', '.join(sorted(unknown))}")
        if "status" not in values:
            raise ValueError("缺少 status")
        return review_semantic_candidate(
            self.database(),
            recording_id,
            candidate_id,
            status=str(values["status"]),
            title=str(values["title"]) if values.get("title") is not None else None,
            body=str(values["body"]) if values.get("body") is not None else None,
            note=str(values["note"]) if values.get("note") is not None else None,
        )

    def start_daily_run(self, recording_id: int) -> dict:
        self.database().get_recording(recording_id)
        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "recording_id": recording_id,
            "status": "queued",
            "stage": "queued",
            "detail": "等待开始",
            "result": None,
            "error": None,
        }
        with self.jobs_lock:
            self.jobs[job_id] = job

        def worker() -> None:
            self._update_job(job_id, status="running", stage="starting", detail="读取配置")

            def progress(stage: str, detail: str) -> None:
                self._update_job(job_id, stage=stage, detail=detail)

            try:
                summary = run_daily(
                    self.database(),
                    recording_id,
                    load_config(self.config_path),
                    progress=progress,
                )
                result = {
                    "run_id": summary.run_id,
                    "recording_id": summary.recording_id,
                    "status": summary.status,
                    "steps": [asdict(step) for step in summary.steps],
                    "review_actions": summary.review_actions,
                    "manifest_markdown_path": str(
                        summary.manifest_markdown_path.resolve()
                    ),
                }
                self._update_job(
                    job_id,
                    status="completed",
                    stage="completed",
                    detail="一键离线日记已完成",
                    result=result,
                )
            except Exception as exc:
                self._update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    detail="运行失败",
                    error=str(exc),
                )

        threading.Thread(target=worker, name=f"daily-run-{job_id[:8]}", daemon=True).start()
        return dict(job)

    def job(self, job_id: str) -> dict:
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"任务 {job_id} 不存在")
            return dict(job)

    def _update_job(self, job_id: str, **values: Any) -> None:
        with self.jobs_lock:
            self.jobs[job_id].update(values)

    def audio_clip(self, segment_id: int, *, mode: str = "segment") -> Path:
        if mode not in {"segment", "context"}:
            raise ValueError("音频试听模式无效")
        database = self.database()
        segment = database.get_segment(segment_id)
        recording = database.get_recording(int(segment["recording_id"]))
        if mode == "context":
            context_ms = 3_000
            start_ms = max(0, int(segment["start_ms"]) - context_ms)
            end_ms = min(
                int(recording["duration_ms"]),
                int(segment["end_ms"]) + context_ms,
            )
            directory = "web-audio-context-v1"
            filename = f"segment-{segment_id}-context.wav"
        else:
            start_ms = int(segment["start_ms"])
            end_ms = int(segment["end_ms"])
            directory = "web-audio-v3"
            filename = f"segment-{segment_id}-listening.wav"
        destination = (
            recording_output_dir(int(recording["id"]))
            / directory
            / filename
        )
        with self.audio_lock:
            if not destination.is_file():
                extract_clip(
                    Path(recording["source_path"]),
                    destination,
                    start_ms,
                    end_ms,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination

    def speaker_timeline_audio_clip(
        self, recording_id: int, *, start_ms: int, end_ms: int
    ) -> Path:
        database = self.database()
        database.get_recording(recording_id)
        session = database.get_session_for_recording(recording_id)
        duration_ms = int(session["duration_ms"])
        if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
            raise ValueError("试听时间范围无效")
        if end_ms - start_ms > MAX_AUDIO_WINDOW_MS:
            raise ValueError("单次试听不能超过 120 秒")
        slices, gaps = resolve_session_slices(
            database, int(session["id"]), start_ms, end_ms
        )
        window = LogicalWindow(
            session_id=int(session["id"]),
            index=0,
            core_start_ms=start_ms,
            core_end_ms=end_ms,
            analysis_start_ms=start_ms,
            analysis_end_ms=end_ms,
            slices=slices,
            uncovered_ranges=gaps,
        )
        transform = "pcm16-16khz-mono-loudnorm-i18-v1"
        fingerprint = logical_window_cache_key(window, transform=transform)[:16]
        destination = (
            recording_output_dir(recording_id)
            / "web-speaker-timeline-audio-v1"
            / f"range-{start_ms}-{end_ms}-{fingerprint}.wav"
        )
        with self.audio_lock:
            if not destination.is_file():
                materialize_logical_window(
                    window,
                    destination,
                    audio_filter="loudnorm=I=-18:LRA=7:TP=-2",
                )
        return destination


class AllDayRequestHandler(BaseHTTPRequestHandler):
    server: "AllDayHTTPServer"

    def do_GET(self) -> None:
        self._handle(self._dispatch_get)

    def do_POST(self) -> None:
        self._handle(self._dispatch_post)

    def do_PUT(self) -> None:
        self._handle(self._dispatch_put)

    def _handle(self, callback) -> None:
        try:
            callback()
        except FileNotFoundError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"本地服务错误：{exc}"},
            )

    @property
    def application(self) -> WebApplication:
        return self.server.application

    def _dispatch_get(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/" and self._consume_token(parsed):
            return
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "请从命令输出的安全链接打开网页"})
            return
        if parsed.path == "/":
            self._send_asset("index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/assets/app.js":
            self._send_asset("app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/assets/styles.css":
            self._send_asset("styles.css", "text/css; charset=utf-8")
            return
        if parsed.path == "/api/recordings":
            self._send_json(HTTPStatus.OK, {"recordings": self.application.recordings()})
            return
        if parsed.path == "/api/dashboard":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(HTTPStatus.OK, self.application.dashboard(recording_id))
            return
        if parsed.path == "/api/evaluations":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK,
                {"evaluations": list_evaluation_templates(recording_id)},
            )
            return
        evaluation_match = _match_path(
            parsed.path, r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)"
        )
        if evaluation_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.evaluation(
                    int(evaluation_match["recording_id"]),
                    unquote(evaluation_match["name"]),
                ),
            )
            return
        if parsed.path == "/api/actions":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK, {"actions": self.application.actions(recording_id)}
            )
            return
        if parsed.path == "/api/semantic":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK, self.application.semantic(recording_id)
            )
            return
        if parsed.path == "/api/runs":
            recording_id = _query_int(parsed.query, "recording_id")
            self._send_json(
                HTTPStatus.OK, {"runs": self.application.runs(recording_id)}
            )
            return
        if parsed.path == "/api/speaker-timeline":
            recording_id = _query_int(parsed.query, "recording_id")
            run_id = _query_optional_int(parsed.query, "run_id")
            self._send_json(
                HTTPStatus.OK,
                self.application.speaker_timeline(recording_id, run_id=run_id),
            )
            return
        if parsed.path == "/api/speaker-timeline/window":
            self._send_json(
                HTTPStatus.OK,
                self.application.speaker_timeline_window(
                    _query_int(parsed.query, "recording_id"),
                    run_id=_query_int(parsed.query, "run_id"),
                    start_ms=_query_nonnegative_int(parsed.query, "start_ms"),
                    end_ms=_query_int(parsed.query, "end_ms"),
                ),
            )
            return
        if parsed.path == "/api/speaker-timeline/audio":
            self._send_file(
                self.application.speaker_timeline_audio_clip(
                    _query_int(parsed.query, "recording_id"),
                    start_ms=_query_nonnegative_int(parsed.query, "start_ms"),
                    end_ms=_query_int(parsed.query, "end_ms"),
                ),
                "audio/wav",
            )
            return
        job_match = _match_path(parsed.path, r"/api/jobs/(?P<job_id>[a-f0-9]+)")
        if job_match:
            self._send_json(HTTPStatus.OK, self.application.job(job_match["job_id"]))
            return
        audio_match = _match_path(parsed.path, r"/api/audio/(?P<segment_id>\d+)")
        if audio_match:
            mode = parse_qs(parsed.query).get("mode", ["segment"])[0]
            self._send_file(
                self.application.audio_clip(
                    int(audio_match["segment_id"]), mode=mode
                ),
                "audio/wav",
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "页面不存在"})

    def _dispatch_post(self) -> None:
        if not self._authorized_mutation():
            return
        parsed = urlparse(self.path)
        body = self._read_json()
        if parsed.path == "/api/daily-run":
            recording_id = int(body["recording_id"])
            self._send_json(
                HTTPStatus.ACCEPTED, self.application.start_daily_run(recording_id)
            )
            return
        if parsed.path == "/api/speaker-timeline/identity-review":
            self._send_json(
                HTTPStatus.OK,
                self.application.review_identity_expansion(
                    int(body["recording_id"]),
                    run_id=int(body["run_id"]),
                    candidate_id=str(body["candidate_id"]),
                    status=str(body["status"]),
                    note=str(body["note"]) if body.get("note") is not None else None,
                ),
            )
            return
        if parsed.path == "/api/semantic/generate":
            self._send_json(
                HTTPStatus.OK,
                self.application.generate_semantic(int(body["recording_id"])),
            )
            return
        semantic_review_match = _match_path(
            parsed.path,
            r"/api/semantic/candidates/(?P<candidate_id>\d+)/review",
        )
        if semantic_review_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.review_semantic(
                    int(body["recording_id"]),
                    int(semantic_review_match["candidate_id"]),
                    body,
                ),
            )
            return
        evaluation_match = _match_path(
            parsed.path,
            r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/run",
        )
        if evaluation_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.run_evaluation(
                    int(evaluation_match["recording_id"]),
                    unquote(evaluation_match["name"]),
                ),
            )
            return
        action_match = _match_path(
            parsed.path, r"/api/actions/(?P<candidate_id>\d+)/review"
        )
        if action_match:
            self._send_json(
                HTTPStatus.OK,
                self.application.review_action(
                    int(action_match["candidate_id"]), body
                ),
            )
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})

    def _dispatch_put(self) -> None:
        if not self._authorized_mutation():
            return
        parsed = urlparse(self.path)
        match = _match_path(
            parsed.path,
            r"/api/evaluations/(?P<recording_id>\d+)/(?P<name>[^/]+)/segments/(?P<segment_id>\d+)",
        )
        if not match:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "接口不存在"})
            return
        updated = self.application.update_evaluation_segment(
            int(match["recording_id"]),
            unquote(match["name"]),
            int(match["segment_id"]),
            self._read_json(),
        )
        self._send_json(HTTPStatus.OK, {"segment": updated})

    def _consume_token(self, parsed) -> bool:
        values = parse_qs(parsed.query)
        token = values.get("token", [None])[0]
        if token is None:
            return False
        if not secrets.compare_digest(token, self.application.token):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "安全链接已失效"})
            return True
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header(
            "Set-Cookie",
            f"allday_session={self.application.token}; Path=/; HttpOnly; SameSite=Strict",
        )
        self._security_headers()
        self.end_headers()
        return True

    def _authenticated(self) -> bool:
        header_token = self.headers.get("X-AllDay-Token")
        if header_token and secrets.compare_digest(header_token, self.application.token):
            return True
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        session = cookie.get("allday_session")
        return bool(
            session
            and secrets.compare_digest(session.value, self.application.token)
        )

    def _authorized_mutation(self) -> bool:
        if not self._authenticated():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "未授权"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {
            self.application.base_url,
            f"http://localhost:{self.application.port}",
        }:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "拒绝跨站请求"})
            return False
        return True

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0 or length > MAX_JSON_BODY:
            raise ValueError("请求正文大小无效")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("请求正文不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求正文必须是 JSON 对象")
        return payload

    def _send_asset(self, filename: str, content_type: str) -> None:
        path = ASSET_ROOT / filename
        if not path.is_file():
            raise FileNotFoundError(f"网页资源不存在：{filename}")
        self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

    def _send_file(self, path: Path, content_type: str) -> None:
        self._send_bytes(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            cache_control="private, max-age=3600",
        )

    def _send_json(self, status: HTTPStatus, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send_bytes(status, data, "application/json; charset=utf-8")

    def _send_bytes(
        self,
        status: HTTPStatus,
        data: bytes,
        content_type: str,
        *,
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        self._security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; media-src 'self'; img-src 'self'; frame-ancestors 'none'",
        )

    def log_message(self, format: str, *args) -> None:
        print(f"[web] {self.address_string()} {format % args}")


class AllDayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address, application: WebApplication):
        self.application = application
        super().__init__(server_address, AllDayRequestHandler)


def create_web_server(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
) -> AllDayHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("本地工作台只能绑定 127.0.0.1 或 localhost")
    application = WebApplication(database_path, config_path, token=token)
    server = AllDayHTTPServer((host, port), application)
    bound_host, bound_port = server.server_address[:2]
    application.host = "127.0.0.1" if bound_host in {"0.0.0.0", "::"} else host
    application.port = int(bound_port)
    return server


def serve_web(
    *,
    database_path: Path = DEFAULT_DB_PATH,
    config_path: Path = DEFAULT_CONFIG_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    server = create_web_server(
        database_path=database_path,
        config_path=config_path,
        host=host,
        port=port,
    )
    url = f"{server.application.base_url}/?token={server.application.token}"
    print(f"AllDayRecording 本地工作台：{url}")
    print("仅监听本机；按 Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _query_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    value = int(values[0])
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _query_optional_int(query: str, name: str) -> int | None:
    values = parse_qs(query).get(name)
    if not values:
        return None
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 1:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _query_nonnegative_int(query: str, name: str) -> int:
    values = parse_qs(query).get(name)
    if not values:
        raise ValueError(f"缺少查询参数 {name}")
    try:
        value = int(values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"查询参数 {name} 必须是整数") from exc
    if value < 0:
        raise ValueError(f"查询参数 {name} 不能为负数")
    return value


def _match_path(path: str, pattern: str) -> dict[str, str] | None:
    import re

    match = re.fullmatch(pattern, path)
    return match.groupdict() if match else None


def _json_value(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}


def _processing_run_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "recording_id": int(row["recording_id"]),
        "run_kind": row["run_kind"],
        "status": row["status"],
        "config_sha256": row["config_sha256"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
        "summary": _json_value(row["summary_json"]),
        "artifacts": _json_value(row["artifacts_json"]),
    }


def _action_payload(row) -> dict:
    return {
        "id": int(row["id"]),
        "recording_id": int(row["recording_id"]),
        "type": row["candidate_type"],
        "status": row["status"],
        "title": row["title"],
        "scheduled_at": row["scheduled_at"],
        "time_text": row["time_text"],
        "location": row["location"],
        "confidence": float(row["confidence"]),
        "source_segment_ids": _json_value(row["source_segment_ids_json"]),
        "participants": _json_value(row["participants_json"]),
        "evidence": _json_value(row["evidence_json"]),
    }
