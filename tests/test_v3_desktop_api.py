from __future__ import annotations

import io
import json
import http.cookiejar
import shutil
import struct
import threading
import unittest
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen
from uuid import uuid4

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application import (
    RecordBackupEvidenceCommand,
    SubmitProcessingCommand,
)
from allday_asr.v3.bootstrap import V3CorePaths
from allday_asr.v3.domain import (
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    Artifact,
    CaptureSegment,
    Device,
    DeviceKind,
    DeviceStatus,
    RecordingSession,
    RecordingSessionState,
    SessionManifest,
)
from allday_asr.v3.interfaces.desktop_server import create_v3_desktop_server


TEST_STATE = Path(__file__).parents[1] / "state"


def _wav_bytes(sample: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(struct.pack("<h", sample) * 16_000)
    return output.getvalue()


class V3DesktopApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_STATE / f"desktop-api-{uuid4().hex}"
        self.paths = V3CorePaths.from_state_dir(self.root)
        self.token = "desktop-test-token"
        self.server = create_v3_desktop_server(
            paths=self.paths, port=0, token=self.token
        )
        self._seed()
        self._start_server()

    def tearDown(self) -> None:
        self._stop_server()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_authentication_error_shape_and_static_spa_routes(self) -> None:
        status, payload, headers = self._request("/api/v3/status", authenticated=False)
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "desktop_session_required")
        self.assertEqual(set(payload), {"code", "message", "details", "request_id"})
        self.assertIn("nosniff", headers["X-Content-Type-Options"])

        status, html, _ = self._request("/recordings/session-1?tab=evidence")
        self.assertEqual(status, 200)
        self.assertIn("AllDay Recording V3", html)
        self.assertNotIn("workspace/controller", html)

    def test_segment_classification_route_requires_authentication_and_dispatches_batch(self):
        body = {"selections": [{"utterance_id": "fixture", "revision": 1}], "sound_kind": "non_speech"}
        with patch.object(self.server.application.core.corrections, "classify_segments",
                          return_value={"utterances": []}) as classify:
            status, _, _ = self._request("/api/v3/utterance-classifications",
                method="POST", authenticated=False, body=body)
            self.assertEqual(status, 403)
            classify.assert_not_called()
            status, payload, _ = self._request("/api/v3/utterance-classifications", method="POST", body=body)
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"utterances": []})
            classify.assert_called_once_with(body["selections"], "non_speech")

    def test_short_workspace_link_recovers_missing_or_rotated_session(self) -> None:
        cookie_jar = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookie_jar))
        navigation_headers = {
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Site": "none",
        }
        workspace = f"{self.server.application.base_url}/recordings/session-1?tab=evidence"

        with opener.open(
            Request(workspace, headers=navigation_headers), timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("AllDay Recording V3", response.read().decode("utf-8"))
        with opener.open(
            f"{self.server.application.base_url}/api/v3/status", timeout=5
        ) as response:
            self.assertEqual(json.load(response)["state"], "ready")

        self.server.application.token = "rotated-desktop-test-token"
        navigation_headers["Sec-Fetch-Site"] = "same-origin"
        with opener.open(
            Request(workspace, headers=navigation_headers), timeout=5
        ) as response:
            self.assertEqual(response.status, 200)
        with opener.open(
            f"{self.server.application.base_url}/api/v3/status", timeout=5
        ) as response:
            self.assertEqual(json.load(response)["state"], "ready")

    def test_frontend_can_recover_api_session_but_cross_site_cannot(self) -> None:
        cookie_jar = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(cookie_jar))
        recovery = Request(
            f"{self.server.application.base_url}/api/v3/desktop-session",
            method="POST",
            headers={
                "Origin": self.server.application.base_url,
                "Sec-Fetch-Site": "same-origin",
                "X-AllDay-Desktop-Recovery": "1",
            },
        )
        with opener.open(recovery, timeout=5) as response:
            self.assertEqual(response.status, 204)
        with opener.open(
            f"{self.server.application.base_url}/api/v3/status", timeout=5
        ) as response:
            self.assertEqual(json.load(response)["state"], "ready")

        status, payload, _ = self._request(
            "/api/v3/desktop-session",
            authenticated=False,
            method="POST",
            headers={
                "Origin": "https://example.invalid",
                "Sec-Fetch-Site": "cross-site",
                "X-AllDay-Desktop-Recovery": "1",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "desktop_session_recovery_denied")

    def test_untrusted_host_cannot_reach_loopback_application(self) -> None:
        status, payload, _ = self._request(
            "/", authenticated=False, headers={"Host": "attacker.invalid"}
        )
        self.assertEqual(status, 421)
        self.assertEqual(payload["code"], "invalid_desktop_host")

    def test_queries_keyset_pagination_and_byte_range_media(self) -> None:
        status, value, _ = self._request("/api/v3/status")
        self.assertEqual(status, 200)
        self.assertEqual(value["state"], "ready")

        _, overview, _ = self._request("/api/v3/overview")
        self.assertEqual(overview["counts"]["sessions"], 2)
        self.assertEqual(overview["counts"]["backed_up_sessions"], 1)

        _, first, _ = self._request("/api/v3/recording-sessions?limit=1")
        self.assertEqual(len(first["items"]), 1)
        self.assertIsNotNone(first["next_cursor"])
        _, second, _ = self._request(
            f"/api/v3/recording-sessions?limit=1&cursor={first['next_cursor']}"
        )
        self.assertEqual(len(second["items"]), 1)
        self.assertNotEqual(
            first["items"][0]["session_id"], second["items"][0]["session_id"]
        )

        _, searched, _ = self._request(
            "/api/v3/recording-sessions?limit=100&q=session-2"
        )
        self.assertEqual(
            [item["session_id"] for item in searched["items"]], ["session-2"]
        )
        _, searched_by_date, _ = self._request(
            "/api/v3/recording-sessions?limit=100&q=2026%2F08%2F31"
        )
        self.assertEqual(len(searched_by_date["items"]), 2)
        _, escaped_search, _ = self._request(
            "/api/v3/recording-sessions?limit=100&q=%25"
        )
        self.assertEqual(escaped_search["items"], [])

        _, detail, _ = self._request("/api/v3/recording-sessions/session-1")
        self.assertEqual(detail["session"]["session_id"], "session-1")
        self.assertEqual(detail["segments"][0]["media_id"], self.media_id)

        status, media, headers = self._request(
            f"/api/v3/media/{self.media_id}", headers={"Range": "bytes=2-5"}
        )
        self.assertEqual(status, 206)
        self.assertEqual(media, b"2345")
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_session_audio_assembles_a_continuous_cross_segment_clip(self) -> None:
        status, payload, headers = self._request(
            "/api/v3/recording-sessions/session-1/audio?"
            "start_ms=1500&end_ms=2500"
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers.get_content_type(), "audio/wav")
        self.assertEqual(headers["Accept-Ranges"], "none")
        assert isinstance(payload, bytes)
        with wave.open(io.BytesIO(payload), "rb") as source:
            self.assertEqual(source.getnchannels(), 1)
            self.assertEqual(source.getframerate(), 16_000)
            self.assertAlmostEqual(source.getnframes() / 16_000, 1.0, places=2)
            samples = struct.unpack(
                f"<{source.getnframes()}h", source.readframes(source.getnframes())
            )
        self.assertGreater(sum(samples[:4_000]), 0)
        self.assertLess(sum(samples[-4_000:]), 0)

        status, error, _ = self._request(
            "/api/v3/recording-sessions/session-2/audio?"
            "start_ms=500&end_ms=1500"
        )
        self.assertEqual(status, 404)
        self.assertEqual(error["code"], "not_found")

    def test_review_inbox_aggregates_unresolved_domain_sources(self) -> None:
        person = self.server.application.core.people.create_person("审核人物")
        created_at = "2026-08-31T02:00:00Z"
        with self.server.application.core.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO processing_runs (
                  run_id, session_id, pipeline_version, input_revision, status,
                  config_digest, current_stage, progress, created_at, updated_at,
                  revision
                ) VALUES (
                  'review-run', 'session-1', 'review-test', 1, 'waiting_review',
                  'review-digest', 'publish', 0.8, ?, ?, 1
                )
                """,
                (created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO stage_runs (
                  stage_run_id, run_id, stage, ordinal, optional, status,
                  progress, created_at, updated_at
                ) VALUES (
                  'review-stage', 'review-run', 'publish', 0, 0,
                  'waiting_review', 0.8, ?, ?
                )
                """,
                (created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO person_memory_entries (
                  memory_id, revision, person_id, kind, summary, details_json,
                  source, confidence, confirmation_status, valid_from,
                  valid_until, status, event_id, reminder_event_id,
                  content_sha256, created_by, created_at
                ) VALUES (
                  'review-memory', 1, ?, 'stable_fact', '喜欢喝红茶', '{}',
                  'model', 0.82, 'unconfirmed', ?, NULL, 'active', NULL, NULL,
                  ?, 'model:test', ?
                )
                """,
                (person["person_id"], created_at, "b" * 64, created_at),
            )
            connection.execute(
                """
                INSERT INTO generation_records (
                  generation_id, layer, producer, producer_version, model,
                  prompt_version, extractor_version, input_scope_json,
                  input_sha256, generation_number, status, error, created_at,
                  completed_at
                ) VALUES (
                  'review-generation', 'event', 'review-test', '1',
                  'review-model', '1', '1', '{}', ?, 1, 'succeeded', NULL,
                  ?, ?
                )
                """,
                ("c" * 64, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO structured_change_proposals (
                  proposal_id, generation_id, kind, payload_json,
                  evidence_utterance_ids_json, status, created_at
                ) VALUES (
                  'review-proposal', 'review-generation', 'event_operation', ?,
                  '["utterance-a"]', 'pending', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "operation": "create",
                            "session_id": "session-1",
                            "event_kind": "decision",
                            "expected_revision": 0,
                            "patch": {
                                "title": "决定带上门禁卡",
                                "summary": "出门前带上门禁卡",
                                "confidence": 0.78,
                            },
                        }
                    ),
                    created_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO generation_records (
                  generation_id, layer, producer, producer_version, model,
                  prompt_version, extractor_version, input_scope_json,
                  input_sha256, generation_number, status, error, created_at,
                  completed_at
                ) VALUES (
                  'review-memory-generation', 'memory', 'review-test', '1',
                  'review-model', '1', '1', '{}', ?, 1, 'succeeded', NULL,
                  ?, ?
                )
                """,
                ("d" * 64, created_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO structured_change_proposals (
                  proposal_id, generation_id, kind, payload_json,
                  evidence_utterance_ids_json, status, created_at
                ) VALUES (
                  'review-memory-proposal', 'review-memory-generation',
                  'memory_record', ?, '[]', 'pending', ?
                )
                """,
                (
                    json.dumps(
                        {
                            "session_id": "session-1",
                            "memory_kind": "stable_fact",
                            "subject_type": "person",
                            "subject_id": person["person_id"],
                            "content": "喜欢喝红茶",
                            "input_event_ids": [],
                        }
                    ),
                    created_at,
                ),
            )

        reminder = {
            "candidate_id": "review-reminder",
            "operation": "CREATE_TASK",
            "session_id": "session-1",
            "title": "带上门禁卡",
            "actor_person_id": person["person_id"],
            "scheduled_at": "2026-09-01T01:00:00Z",
            "location": "办公室",
            "confidence": 0.76,
            "expected_revision": None,
            "evidence_utterance_ids": ["utterance-a"],
            "created_at": created_at,
        }
        voice = {
            "prototype_id": "review-prototype",
            "speaker_track_id": "review-track",
            "cluster_id": "review-cluster",
            "session_id": "session-1",
            "quality_score": 0.91,
            "created_at": created_at,
            "person_id": person["person_id"],
            "person_name": person["display_name"],
            "review_status": "pending",
            "review": None,
            "representative_clips": [{"media_id": "media", "start_ms": 0}],
            "decision_tier": "suggested",
            "best_score": 0.88,
            "score_margin": 0.12,
            "match_reason": "known_person_suggested",
        }
        training_voice = {
            **voice,
            "prototype_id": "training-prototype",
            "speaker_track_id": "training-track",
            "cluster_id": "training-cluster",
            "decision_tier": "no_known_match",
            "best_score": 0.76,
            "score_margin": 0.18,
        }
        low_value_voice = {
            **voice,
            "prototype_id": "low-value-prototype",
            "speaker_track_id": "low-value-track",
            "cluster_id": "low-value-cluster",
            "decision_tier": "no_known_match",
            "best_score": 0.53,
            "score_margin": 0.0,
        }
        repository_module = "allday_asr.v3.adapters.sqlite.desktop_repository"
        with (
            patch(
                f"{repository_module}.SqliteReminderRepository.list_candidates",
                return_value=(reminder,),
            ),
            patch(
                f"{repository_module}.SqlitePeopleRepository.list_review_candidates",
                return_value=(voice, training_voice, low_value_voice),
            ),
        ):
            status, payload, _ = self._request("/api/v3/reviews")
            _, people_payload, _ = self._request("/api/v3/persons")

        self.assertEqual(status, 200)
        self.assertEqual(
            {item["kind"] for item in payload["items"]},
            {
                "reminder",
                "voice_identity",
                "knowledge_proposal",
            },
        )
        self.assertEqual(
            [item["priority"] for item in payload["items"]],
            ["high", "normal", "normal"],
        )
        self.assertNotIn(
            "review-memory",
            {item["source_id"] for item in payload["items"]},
        )
        proposal = next(
            item for item in payload["items"] if item["kind"] == "knowledge_proposal"
        )
        self.assertEqual(proposal["title"], "决定带上门禁卡")
        self.assertNotIn(
            "review-memory-proposal",
            {item["source_id"] for item in payload["items"]},
        )
        voice_items = [
            item for item in payload["items"] if item["kind"] == "voice_identity"
        ]
        self.assertEqual(len(voice_items), 1)
        self.assertEqual(
            {item["context"]["review_lane"] for item in voice_items},
            {"primary"},
        )
        self.assertNotIn(
            "training-prototype",
            {
                prototype_id
                for item in voice_items
                for prototype_id in item["context"]["prototype_ids"]
            },
        )
        self.assertNotIn(
            "low-value-prototype",
            {
                prototype_id
                for item in voice_items
                for prototype_id in item["context"]["prototype_ids"]
            },
        )
        person_summary = next(
            item
            for item in people_payload["items"]
            if item["person_id"] == person["person_id"]
        )
        self.assertEqual(person_summary["pending_voice_review_count"], 1)
        self.assertEqual(person_summary["training_voice_review_count"], 1)

    def test_workflow_failure_stays_out_of_review_inbox_and_can_be_retried(self) -> None:
        self.server.application.automatic_workflows.save(
            "session-1",
            {
                "version": 1,
                "session_id": "session-1",
                "status": "needs_attention",
                "stage": "backup",
                "detail": "自动重试仍未成功",
                "error": "backup device unavailable",
                "attempt_count": 4,
                "auto_retry_count": 3,
                "max_auto_retries": 3,
                "created_at": "2026-09-02T00:00:00Z",
                "updated_at": "2026-09-02T00:03:00Z",
            },
        )

        status, payload, _ = self._request("/api/v3/reviews")
        self.assertEqual(status, 200)
        self.assertNotIn("workflow_failure", {item["kind"] for item in payload["items"]})

        status, accepted, _ = self._request(
            "/api/v3/automatic-workflows/session-1/retry",
            method="POST",
            body={},
        )
        self.assertEqual(status, 202)
        self.assertEqual(accepted["status"], "retry_requested")
        self.assertEqual(
            self.server.application.automatic_workflows.status("session-1")["status"],
            "retry_requested",
        )

    def test_durable_job_command_sse_projection_and_restart_recovery(self) -> None:
        snapshot = self.server.application.core.processing.submit(
            SubmitProcessingCommand(
                session_id="session-1",
                pipeline_version="v3-desktop-test.1",
                input_revision=1,
                config={"profile": "desktop-test"},
            )
        )
        job_id = snapshot.job.job_id
        with SqliteUnitOfWork(self.server.application.core.database) as uow:
            uow.artifacts.add(
                Artifact(
                    artifact_id="artifact-missing",
                    run_id=snapshot.run.run_id,
                    kind="legacy_missing",
                    producer="desktop-test",
                    producer_version="1",
                    config_digest=snapshot.run.config_digest,
                    input_refs=(),
                    storage_ref="legacy-missing:test",
                    sha256=None,
                    size_bytes=None,
                    status="quarantined",
                    metadata={},
                    created_at=datetime.now(timezone.utc),
                )
            )
        detail = self.server.application.core.desktop.session_detail("session-1")
        missing = next(
            item for item in detail["artifacts"] if item["artifact_id"] == "artifact-missing"
        )
        self.assertIsNone(missing["sha256"])
        self.assertIsNone(missing["size_bytes"])
        _, jobs, _ = self._request("/api/v3/processing-jobs")
        self.assertEqual(jobs["items"][0]["job_id"], job_id)
        events = self.server.application.core.desktop.processing_events(0)
        self.assertTrue(any(event["resource_id"] == snapshot.run.run_id for event in events))

        status, cancelled, _ = self._request(
            f"/api/v3/processing-jobs/{job_id}/cancel",
            method="POST",
            body={"reason": "desktop test"},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 202)
        self.assertIn(cancelled["job"]["status"], {"cancel_requested", "cancelled"})

        self._stop_server()
        self.server = create_v3_desktop_server(
            paths=self.paths, port=0, token=self.token
        )
        self._start_server()
        _, recovered, _ = self._request(f"/api/v3/processing-jobs/{job_id}")
        self.assertEqual(recovered["job"]["job_id"], job_id)
        self.assertIn(recovered["job"]["status"], {"cancel_requested", "cancelled"})

    def test_mutations_reject_cross_origin_requests(self) -> None:
        status, payload, _ = self._request(
            "/api/v3/processing-jobs/missing/cancel",
            method="POST",
            body={"reason": "denied"},
            headers={"Origin": "https://example.invalid"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "cross_origin_denied")

    def test_v34_people_routes_keep_unknown_legal_and_audio_local(self) -> None:
        status, people, _ = self._request("/api/v3/persons")
        self.assertEqual(status, 200)
        self.assertEqual(people["items"], [])
        status, person, _ = self._request(
            "/api/v3/persons",
            method="POST",
            body={"display_name": "张老师"},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 201)
        self.assertEqual(person["display_name"], "张老师")
        _, people, _ = self._request("/api/v3/persons")
        self.assertEqual(people["items"][0]["voice_maturity_status"], "seed")
        self.assertEqual(people["items"][0]["pending_voice_review_count"], 0)
        _, candidates, _ = self._request("/api/v3/voice-prototype-candidates")
        self.assertEqual(candidates["items"], [])
        status, rematched, _ = self._request(
            "/api/v3/speaker-clusters/rematch",
            method="POST",
            body={},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 200)
        self.assertEqual(rematched["rematched_prototype_count"], 0)
        status, payload, _ = self._request(
            f"/api/v3/persons/{person['person_id']}/identity-policy",
            method="POST",
            body={"auto_match_enabled": True},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_request")
        _, clusters, _ = self._request("/api/v3/speaker-clusters")
        self.assertEqual(clusters["items"], [])
        status, run, _ = self._request(
            "/api/v3/speaker-cluster-runs",
            method="POST",
            body={"session_id": "session-1"},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 200)
        self.assertEqual(run["embedded_track_count"], 0)
        _, settings, _ = self._request("/api/v3/settings")
        self.assertTrue(settings["speaker_identity"]["unknown_is_legal"])
        self.assertEqual(settings["speaker_identity"]["audio_processing"], "local_only")
        status, html, _ = self._request("/people")
        self.assertEqual(status, 200)
        self.assertIn("AllDay Recording V3", html)

    def test_v32_three_layer_routes_start_empty_and_reject_unproven_events(
        self,
    ) -> None:
        for path in (
            "/api/v3/evidence-spans?session_id=session-1",
            "/api/v3/events?session_id=session-1",
            "/api/v3/memories?session_id=session-1",
            "/api/v3/knowledge-proposals",
            "/api/v3/invalidations",
            "/api/v3/recompute-requests",
            "/api/v3/reminder-candidates",
            "/api/v3/reminders",
            "/api/v3/reminders/due",
        ):
            status, payload, _ = self._request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(payload["items"], [], path)

        status, payload, _ = self._request(
            "/api/v3/knowledge-generations",
            method="POST",
            body={
                "layer": "event",
                "producer": "desktop-test",
                "producer_version": "1",
                "model": "fixture",
                "prompt_version": "1",
                "extractor_version": "1",
                "input_scope": {"session_id": "session-1"},
                "proposals": [
                    {
                        "kind": "event_operation",
                        "payload": {
                            "operation": "create",
                            "session_id": "session-1",
                            "event_kind": "task",
                            "expected_revision": 0,
                            "patch": {"title": "unproven"},
                        },
                        "evidence_utterance_ids": ["missing-utterance"],
                    }
                ],
            },
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_request")

        status, payload, _ = self._request(
            "/api/v3/reminder-generations",
            method="POST",
            body={
                "producer": "desktop-test",
                "producer_version": "1",
                "model": "fixture",
                "prompt_version": "1",
                "extractor_version": "1",
                "input_scope": {"session_id": "session-1"},
                "intents": [
                    {
                        "operation": "CREATE_TASK",
                        "session_id": "session-1",
                        "title": "unproven reminder",
                        "actor_person_id": "self",
                        "commitment_direction": "self_to_other",
                        "related_person_ids": [],
                        "scheduled_at": "2026-09-02T10:00:00+08:00",
                        "confidence": 0.9,
                        "evidence_utterance_ids": ["missing-utterance"],
                        "needs_confirmation": True,
                    }
                ],
            },
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_request")

        status, payload, _ = self._request(
            "/api/v3/reminder-generations/codex",
            method="POST",
            body={"session_id": "session-1", "reasoning_effort": "auto"},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_request")

    def test_v35_person_memory_routes_keep_evidence_and_profile_boundaries(self) -> None:
        _, person, _ = self._request(
            "/api/v3/persons",
            method="POST",
            body={"display_name": "张同学"},
            headers={"Origin": self.server.application.base_url},
        )
        person_id = person["person_id"]
        status, detail, _ = self._request(f"/api/v3/persons/{person_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["memory_count"], 0)
        self.assertEqual(detail["interactions"], [])

        status, profile, _ = self._request(
            f"/api/v3/persons/{person_id}/profile",
            method="POST",
            body={
                "display_name": "张老师",
                "aliases": ["老张", "张同学"],
                "relationship_labels": ["项目成员"],
                "notes": "合同项目",
            },
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 200)
        self.assertEqual(profile["aliases"], ["老张", "张同学"])

        status, refresh, _ = self._request(
            f"/api/v3/persons/{person_id}/memories/refresh",
            method="POST",
            body={},
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 200)
        self.assertEqual(refresh["created_count"], 0)

        status, error, _ = self._request(
            f"/api/v3/persons/{person_id}/memories",
            method="POST",
            body={
                "kind": "stable_fact",
                "summary": "没有证据的事实",
                "valid_from": "2026-09-01T08:00:00+00:00",
            },
            headers={"Origin": self.server.application.base_url},
        )
        self.assertEqual(status, 400)
        self.assertEqual(error["code"], "invalid_request")

    def _start_server(self) -> None:
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()

    def _stop_server(self) -> None:
        server = getattr(self, "server", None)
        thread = getattr(self, "thread", None)
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        self.thread = None

    def _request(
        self,
        path: str,
        *,
        authenticated: bool = True,
        method: str = "GET",
        body: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, object, object]:
        request_headers = dict(headers or {})
        if authenticated:
            request_headers["X-AllDay-Desktop-Session"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.server.application.base_url}{path}",
            data=data,
            headers=request_headers,
            method=method,
        )
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            content_type = response.headers.get_content_type()
            if content_type == "application/json":
                decoded: object = json.loads(payload)
            elif content_type.startswith("text/"):
                decoded = payload.decode("utf-8")
            else:
                decoded = payload
            return response.status, decoded, response.headers

    def _seed(self) -> None:
        core = self.server.application.core
        now = datetime(2026, 8, 31, 1, 5, tzinfo=timezone.utc)
        audio = core.audio_store.put_bytes(b"0123456789")
        clip_audio = (
            core.audio_store.put_bytes(_wav_bytes(1000)),
            core.audio_store.put_bytes(_wav_bytes(-1000)),
        )
        self.media_id = audio.media_id
        manifests = [
            core.artifact_store.put_bytes(
                json.dumps({"session_id": session_id}).encode("utf-8")
            )
            for session_id in ("session-1", "session-2")
        ]
        with SqliteUnitOfWork(core.database) as uow:
            uow.devices.add(
                Device(
                    device_id="computer-1",
                    kind=DeviceKind.COMPUTER,
                    name="Desktop",
                    status=DeviceStatus.ACTIVE,
                    revision=1,
                    last_seen_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id="asset-1",
                    sha256=audio.sha256,
                    size_bytes=audio.size_bytes,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id=audio.media_id,
                    created_at=now,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id="replica-1",
                    asset_id="asset-1",
                    device_id="computer-1",
                    storage_key=audio.storage_key,
                    state=AudioReplicaState.AVAILABLE,
                    verified_at=now,
                    created_at=now,
                )
            )
            for index, stored in enumerate(clip_audio, start=2):
                uow.catalog.add_asset(
                    AudioAsset(
                        asset_id=f"asset-{index}",
                        sha256=stored.sha256,
                        size_bytes=stored.size_bytes,
                        duration_ms=1000,
                        format=AudioFormat.WAV,
                        media_id=stored.media_id,
                        created_at=now,
                    )
                )
                uow.catalog.add_replica(
                    AudioReplica(
                        replica_id=f"replica-{index}",
                        asset_id=f"asset-{index}",
                        device_id="computer-1",
                        storage_key=stored.storage_key,
                        state=AudioReplicaState.AVAILABLE,
                        verified_at=now,
                        created_at=now,
                    )
                )
            for index, session_id in enumerate(("session-1", "session-2")):
                captured = now - timedelta(hours=index)
                uow.catalog.add_session(
                    RecordingSession(
                        session_id=session_id,
                        captured_start=captured,
                        captured_end=captured + timedelta(seconds=1),
                        timezone="Asia/Singapore",
                        state=RecordingSessionState.COMPUTER_INGESTED,
                        revision=1,
                        status_code="backup_required",
                        current_stage="ingest",
                        progress=1.0,
                        blocking_reason="backup_required",
                        created_at=captured,
                        updated_at=captured,
                    )
                )
                uow.catalog.add_segment(
                    CaptureSegment(
                        segment_id=f"segment-{index + 1}",
                        session_id=session_id,
                        asset_id="asset-1",
                        replica_id="replica-1",
                        sequence=0,
                        session_start_ms=0,
                        session_end_ms=1000,
                        source_start_ms=0,
                        source_end_ms=1000,
                        start_sample=0,
                        captured_at=captured,
                    )
                )
                manifest = manifests[index]
                uow.catalog.add_manifest(
                    SessionManifest(
                        manifest_id=f"manifest-{index + 1}",
                        session_id=session_id,
                        schema_version="1",
                        sha256=manifest.sha256,
                        storage_ref=manifest.storage_key,
                        entries={"session_id": session_id},
                        created_at=captured,
                    )
                )
            for index in range(2):
                uow.catalog.add_segment(
                    CaptureSegment(
                        segment_id=f"assembled-segment-{index + 1}",
                        session_id="session-1",
                        asset_id=f"asset-{index + 2}",
                        replica_id=f"replica-{index + 2}",
                        sequence=index + 1,
                        session_start_ms=(index + 1) * 1000,
                        session_end_ms=(index + 2) * 1000,
                        source_start_ms=0,
                        source_end_ms=1000,
                        start_sample=(index + 1) * 16_000,
                        captured_at=now + timedelta(seconds=index + 1),
                    )
                )
        admitted = core.admission.record_verified_backup(
            RecordBackupEvidenceCommand(
                session_id="session-1",
                provider="desktop-test",
                storage_kind="independent_device",
                digest="a" * 64,
                restore_checked_at=now,
                metadata={"restore_drill": True},
            )
        )
        self.assertTrue(admitted)


if __name__ == "__main__":
    unittest.main()
