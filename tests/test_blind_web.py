from __future__ import annotations

import hashlib
import http.cookiejar
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

from allday_asr.blind_web import create_blind_annotation_server
from allday_asr.services.benchmark import (
    BLIND_PROTOCOL_FORMAT,
    CONTINUOUS_TRUTH_FORMAT,
)


class BlindAnnotationWebTests(unittest.TestCase):
    def test_isolated_editor_auth_range_annotation_and_finalize(self) -> None:
        root = Path(__file__).parent / f"blind-web-{uuid4().hex}"
        root.mkdir()
        task_path = root / "truth-draft.jsonl"
        audio_path = root / "window-000.wav"
        audio_bytes = b"RIFF" + bytes(range(64))
        audio_path.write_bytes(audio_bytes)
        rows = [
            {
                "type": "metadata",
                "format": CONTINUOUS_TRUTH_FORMAT,
                "name": "blind-web-test",
                "session_id": 1,
                "scope_start_ms": 10_000,
                "scope_end_ms": 15_000,
                "input_fingerprint": "test-fingerprint",
                "completeness": {
                    "vad": "pending",
                    "transcript": "pending",
                    "speaker": "none",
                    "alignment": "none",
                    "entities": "none",
                },
                "provenance": {
                    "kind": "blind_continuous_annotation_v1",
                    "protocol": BLIND_PROTOCOL_FORMAT,
                    "model_outputs_used_for_selection": False,
                },
                "blind_attestation": {
                    "model_outputs_unseen": False,
                    "annotator": "",
                    "completed_at": None,
                },
            },
            {
                "type": "blind_window",
                "window_index": 0,
                "session_start_ms": 10_000,
                "session_end_ms": 15_000,
                "audio_file": audio_path.name,
                "audio_sha256": hashlib.sha256(audio_bytes).hexdigest(),
                "review_status": "pending",
                "notes": "",
            },
            {
                "type": "annotation",
                "key": "blind:review-region:0000",
                "kind": "uncertain",
                "session_start_ms": 10_000,
                "session_end_ms": 15_000,
                "label": "review_region_complete_scope",
                "text": None,
                "metadata": {"protocol": BLIND_PROTOCOL_FORMAT},
            },
        ]
        task_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
            encoding="utf-8",
        )
        server = create_blind_annotation_server(
            task_path, port=0, token="blind-test-token"
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = server.application.base_url
        try:
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(f"{base_url}/api/task", timeout=3)
            self.assertEqual(denied.exception.code, 403)

            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar)
            )
            with opener.open(
                f"{base_url}/?token=blind-test-token", timeout=3
            ) as response:
                html = response.read().decode("utf-8")
            self.assertIn("V2-C 独立盲标台", html)
            self.assertNotIn("hypothesis", html)

            with opener.open(f"{base_url}/api/task", timeout=3) as response:
                task = json.load(response)
            self.assertFalse(task["finalized"])
            self.assertEqual(task["windows"][0]["utterance_count"], 0)

            range_request = urllib.request.Request(
                f"{base_url}/audio/0", headers={"Range": "bytes=0-3"}
            )
            with opener.open(range_request, timeout=3) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), b"RIFF")

            utterance = self._post(
                f"{base_url}/api/utterances",
                {
                    "window_index": 0,
                    "start_ms": 500,
                    "end_ms": 1_500,
                    "text": "测试文字",
                    "unintelligible": False,
                },
            )["utterance"]
            self.assertEqual(utterance["text"], "测试文字")
            self._post(
                f"{base_url}/api/windows/0/status", {"status": "complete"}
            )
            finalized = self._post(
                f"{base_url}/api/finalize",
                {"annotator": "unit-test", "confirm_unseen": True},
            )
            self.assertTrue(finalized["finalized"])
            self.assertEqual(finalized["completeness"]["vad"], "exhaustive")
            saved = task_path.read_text(encoding="utf-8")
            self.assertIn('"kind": "speech"', saved)
            self.assertIn('"kind": "transcript"', saved)
            self.assertNotIn("hypothesis_text", saved)

            with self.assertRaises(urllib.error.HTTPError) as locked:
                self._post(
                    f"{base_url}/api/utterances/{utterance['utterance_id']}/delete",
                    {},
                )
            self.assertEqual(locked.exception.code, 400)

            contaminated = [json.loads(line) for line in saved.splitlines()]
            contaminated.append(
                {"type": "annotation", "hypothesis_text": "must never be shown"}
            )
            task_path.write_text(
                "\n".join(json.dumps(row) for row in contaminated) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "被禁止的模型/V1 字段"):
                create_blind_annotation_server(
                    task_path, port=0, token="second-test-token"
                )
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(root)

    @staticmethod
    def _post(url: str, payload: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-AllDay-Token": "blind-test-token",
            },
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)


if __name__ == "__main__":
    unittest.main()
