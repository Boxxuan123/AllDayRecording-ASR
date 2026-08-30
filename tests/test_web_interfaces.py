from __future__ import annotations

import threading
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

from allday_asr.interfaces.web.auth import TokenAuthMixin
from allday_asr.interfaces.web.jobs import JobRegistry
from allday_asr.interfaces.web.params import (
    body_target,
    match_path,
    query_int,
    query_nonnegative_int,
    query_optional_int,
    query_target,
)
from allday_asr.interfaces.web.router import (
    dispatch_get,
    dispatch_post,
    dispatch_put,
)
from allday_asr.interfaces.web.routes.actions import get_actions, post_actions
from allday_asr.interfaces.web.routes.assets import get_asset
from allday_asr.interfaces.web.routes.audio import get_audio
from allday_asr.interfaces.web.routes.evaluation import (
    get_evaluation,
    post_evaluation,
    put_evaluation,
)
from allday_asr.interfaces.web.routes.semantic import get_semantic, post_semantic
from allday_asr.interfaces.web.routes.timeline import get_timeline, post_timeline
from allday_asr.interfaces.web.routes.workspace import (
    get_workspace,
    post_workspace,
)


class _RouteHandler:
    def __init__(
        self,
        path: str = "/",
        *,
        authenticated: bool = True,
        authorized_mutation: bool = True,
        body: dict | None = None,
    ) -> None:
        self.path = path
        self.application = MagicMock()
        self.sent_json: list[tuple[HTTPStatus, object]] = []
        self.sent_files: list[tuple[object, str]] = []
        self.sent_assets: list[tuple[str, str]] = []
        self.read_json_calls = 0
        self._authenticated_value = authenticated
        self._authorized_mutation_value = authorized_mutation
        self.body = body or {}

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        self.sent_json.append((status, payload))

    def _send_file(self, path: object, content_type: str) -> None:
        self.sent_files.append((path, content_type))

    def _send_asset(self, filename: str, content_type: str) -> None:
        self.sent_assets.append((filename, content_type))

    def _consume_token(self, parsed) -> bool:
        return False

    def _authenticated(self) -> bool:
        return self._authenticated_value

    def _authorized_mutation(self) -> bool:
        return self._authorized_mutation_value

    def _read_json(self) -> dict:
        self.read_json_calls += 1
        return self.body


class _AuthHarness(TokenAuthMixin):
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.application = SimpleNamespace(
            token="test-token",
            base_url="http://127.0.0.1:8765",
            port=8765,
        )
        self.sent_json: list[tuple[HTTPStatus, object]] = []

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        self.sent_json.append((status, payload))


class WebParameterTests(unittest.TestCase):
    def test_parameter_parsers_validate_ids_targets_and_paths(self) -> None:
        self.assertEqual(query_int("recording_id=4", "recording_id"), 4)
        self.assertEqual(query_optional_int("", "run_id"), None)
        self.assertEqual(query_target("session_id=8"), (None, 8))
        self.assertEqual(body_target({"recording_id": 3}), (3, None))
        self.assertEqual(query_nonnegative_int("start_ms=0", "start_ms"), 0)
        self.assertEqual(
            match_path("/api/jobs/deadbeef", r"/api/jobs/(?P<job_id>[a-f0-9]+)"),
            {"job_id": "deadbeef"},
        )

        invalid_calls = (
            lambda: query_int("", "recording_id"),
            lambda: query_int("recording_id=0", "recording_id"),
            lambda: query_optional_int("run_id=bad", "run_id"),
            lambda: query_target(""),
            lambda: body_target({}),
            lambda: body_target({"session_id": -1}),
            lambda: query_nonnegative_int("start_ms=-1", "start_ms"),
        )
        for invalid_call in invalid_calls:
            with self.subTest(call=invalid_call), self.assertRaises(ValueError):
                invalid_call()


class WebRouteTests(unittest.TestCase):
    def test_resource_get_routes_delegate_and_preserve_payloads(self) -> None:
        handler = _RouteHandler()
        handler.application.recordings.return_value = [{"id": 1}]
        self.assertTrue(get_workspace(handler, urlparse("/api/recordings")))
        self.assertEqual(
            handler.sent_json[-1],
            (HTTPStatus.OK, {"recordings": [{"id": 1}]}),
        )

        handler = _RouteHandler()
        handler.application.evaluation.return_value = {"name": "smoke"}
        self.assertTrue(
            get_evaluation(handler, urlparse("/api/evaluations/2/smoke"))
        )
        handler.application.evaluation.assert_called_once_with(2, "smoke")

        handler = _RouteHandler()
        handler.application.actions.return_value = [{"id": 3}]
        self.assertTrue(get_actions(handler, urlparse("/api/actions?recording_id=2")))
        self.assertEqual(handler.sent_json[-1][1], {"actions": [{"id": 3}]})

        handler = _RouteHandler()
        handler.application.semantic.return_value = {"available": True}
        self.assertTrue(
            get_semantic(handler, urlparse("/api/semantic?session_id=5"))
        )
        handler.application.semantic.assert_called_once_with(None, session_id=5)

        handler = _RouteHandler()
        handler.application.speaker_timeline.return_value = {"available": False}
        self.assertTrue(
            get_timeline(
                handler,
                urlparse("/api/speaker-timeline?recording_id=2&run_id=7"),
            )
        )
        handler.application.speaker_timeline.assert_called_once_with(
            2,
            session_id=None,
            run_id=7,
        )

        handler = _RouteHandler()
        clip = object()
        handler.application.audio_clip.return_value = clip
        self.assertTrue(get_audio(handler, urlparse("/api/audio/9?mode=context")))
        handler.application.audio_clip.assert_called_once_with(9, mode="context")
        self.assertEqual(handler.sent_files, [(clip, "audio/wav")])

        handler = _RouteHandler()
        self.assertTrue(get_asset(handler, urlparse("/assets/app.js")))
        self.assertEqual(handler.sent_assets[0][0], "assets/app.js")

    def test_resource_mutation_routes_delegate_and_preserve_statuses(self) -> None:
        handler = _RouteHandler()
        handler.application.start_daily_run.return_value = {"status": "queued"}
        self.assertTrue(
            post_workspace(
                handler,
                urlparse("/api/daily-run"),
                {"recording_id": 2},
            )
        )
        self.assertEqual(handler.sent_json[-1][0], HTTPStatus.ACCEPTED)

        handler = _RouteHandler()
        handler.application.create_session_evaluation.return_value = {"run_id": 4}
        self.assertTrue(
            post_evaluation(
                handler,
                urlparse("/api/session-evaluation/v2d1"),
                {"session_id": 3, "run_id": 4},
            )
        )

        handler = _RouteHandler()
        handler.application.update_evaluation_segment.return_value = {"id": 6}
        self.assertTrue(
            put_evaluation(
                handler,
                urlparse("/api/evaluations/2/smoke/segments/6"),
                {"include": True},
            )
        )
        self.assertEqual(handler.sent_json[-1][1], {"segment": {"id": 6}})

        handler = _RouteHandler()
        handler.application.review_action.return_value = {"id": 8}
        self.assertTrue(
            post_actions(
                handler,
                urlparse("/api/actions/8/review"),
                {"status": "accepted"},
            )
        )

        handler = _RouteHandler()
        handler.application.generate_semantic.return_value = {"run_id": 9}
        self.assertTrue(
            post_semantic(
                handler,
                urlparse("/api/semantic/generate"),
                {"session_id": 3},
            )
        )
        handler.application.generate_semantic.assert_called_once_with(
            None,
            session_id=3,
        )

        handler = _RouteHandler()
        handler.application.review_possible_speech.return_value = {"status": "kept"}
        self.assertTrue(
            post_timeline(
                handler,
                urlparse("/api/speaker-timeline/possible-review"),
                {
                    "recording_id": 2,
                    "run_id": 4,
                    "candidate_id": "candidate-1",
                    "status": "kept",
                },
            )
        )

    def test_resource_routes_reject_bad_parameters_and_ignore_unknown_paths(self) -> None:
        bad_routes = (
            lambda: get_workspace(
                _RouteHandler(), urlparse("/api/dashboard?recording_id=bad")
            ),
            lambda: get_evaluation(
                _RouteHandler(), urlparse("/api/evaluations?recording_id=0")
            ),
            lambda: get_actions(_RouteHandler(), urlparse("/api/actions")),
            lambda: get_semantic(_RouteHandler(), urlparse("/api/semantic")),
            lambda: get_timeline(
                _RouteHandler(),
                urlparse(
                    "/api/speaker-timeline/window?"
                    "recording_id=2&run_id=1&start_ms=-1&end_ms=10"
                ),
            ),
        )
        for bad_route in bad_routes:
            with self.subTest(route=bad_route), self.assertRaises(ValueError):
                bad_route()

        parsed = urlparse("/api/not-a-resource")
        self.assertFalse(get_workspace(_RouteHandler(), parsed))
        self.assertFalse(get_evaluation(_RouteHandler(), parsed))
        self.assertFalse(get_actions(_RouteHandler(), parsed))
        self.assertFalse(get_semantic(_RouteHandler(), parsed))
        self.assertFalse(get_timeline(_RouteHandler(), parsed))
        self.assertFalse(get_audio(_RouteHandler(), parsed))
        self.assertFalse(get_asset(_RouteHandler(), parsed))


class WebRouterAndAuthTests(unittest.TestCase):
    def test_router_owns_unauthorized_and_not_found_responses(self) -> None:
        denied = _RouteHandler("/api/recordings", authenticated=False)
        dispatch_get(denied)
        self.assertEqual(denied.sent_json[-1][0], HTTPStatus.FORBIDDEN)
        denied.application.recordings.assert_not_called()

        denied_mutation = _RouteHandler(
            "/api/daily-run",
            authorized_mutation=False,
            body={"recording_id": 1},
        )
        dispatch_post(denied_mutation)
        self.assertEqual(denied_mutation.read_json_calls, 0)

        missing_get = _RouteHandler("/api/not-a-resource")
        dispatch_get(missing_get)
        self.assertEqual(missing_get.sent_json[-1][0], HTTPStatus.NOT_FOUND)

        missing_post = _RouteHandler("/api/not-a-resource", body={})
        dispatch_post(missing_post)
        self.assertEqual(missing_post.sent_json[-1][0], HTTPStatus.NOT_FOUND)

        missing_put = _RouteHandler("/api/not-a-resource", body={})
        dispatch_put(missing_put)
        self.assertEqual(missing_put.sent_json[-1][0], HTTPStatus.NOT_FOUND)

    def test_token_auth_accepts_local_mutation_and_rejects_cross_origin(self) -> None:
        local = _AuthHarness(
            {
                "X-AllDay-Token": "test-token",
                "Origin": "http://127.0.0.1:8765",
            }
        )
        self.assertTrue(local._authorized_mutation())
        self.assertEqual(local.sent_json, [])

        cross_origin = _AuthHarness(
            {
                "X-AllDay-Token": "test-token",
                "Origin": "https://example.invalid",
            }
        )
        self.assertFalse(cross_origin._authorized_mutation())
        self.assertEqual(cross_origin.sent_json[-1][0], HTTPStatus.FORBIDDEN)


class JobRegistryTests(unittest.TestCase):
    def test_registry_tracks_queued_running_completed_failed_and_missing(self) -> None:
        registry = JobRegistry()
        completed = registry.create(kind="daily")
        completed_id = str(completed["id"])
        self.assertEqual(completed["status"], "queued")
        registry.update(
            completed_id,
            status="running",
            stage="processing",
            detail="working",
        )
        self.assertEqual(registry.get(completed_id)["status"], "running")
        registry.update(
            completed_id,
            status="completed",
            stage="completed",
            result={"run_id": 7},
        )
        self.assertEqual(registry.get(completed_id)["result"], {"run_id": 7})

        failed = registry.create(kind="daily")
        registry.update(
            str(failed["id"]),
            status="failed",
            stage="failed",
            error="boom",
        )
        self.assertEqual(registry.get(str(failed["id"]))["status"], "failed")

        launched = threading.Event()
        thread = registry.launch(target=launched.set, name="job-registry-test")
        thread.join(timeout=2)
        self.assertTrue(launched.is_set())
        with self.assertRaises(KeyError):
            registry.get("missing")


if __name__ == "__main__":
    unittest.main()
