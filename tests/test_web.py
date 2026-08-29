from __future__ import annotations

import http.cookiejar
import json
import shutil
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from allday_asr.services.evaluation import create_evaluation_template
from allday_asr.storage.database import Database
from allday_asr.web import create_web_server


class WebConsoleTests(unittest.TestCase):
    def test_local_console_authentication_dashboard_and_annotation_update(self) -> None:
        suffix = uuid4().hex
        root = Path(__file__).parent
        database_path = root / f"web-{suffix}.sqlite3"
        evaluation_dir = root / f"web-evaluation-{suffix}"
        web_output = root / f"web-output-{suffix}"
        server = None
        try:
            database = Database(database_path)
            recording = database.create_recording(
                {
                    "source_path": str(root / "test-audio.m4a"),
                    "sha256": f"web-{suffix}",
                    "device": "test",
                    "recorded_at": "2026-08-28T08:00:00+08:00",
                    "timezone": "Asia/Singapore",
                    "duration_ms": 5_000,
                    "codec": "aac",
                    "sample_rate": 16_000,
                    "channels": 1,
                    "bit_rate": 64_000,
                    "encoder": "test",
                }
            )
            recording_id = int(recording["id"])
            database.replace_vad_segments(
                recording_id, [(500, 2_500)], str(root / "test-audio.m4a")
            )
            segment = database.all_segments(recording_id)[0]
            segment_id = int(segment["id"])
            database.mark_segment_running(segment_id)
            database.mark_segment_completed(
                segment_id,
                language="zh",
                text_raw="明天十点见",
                text_display="明天十点见",
                asr_model="test",
            )

            with patch("allday_asr.services.evaluation.EVALUATION_DIR", evaluation_dir):
                create_evaluation_template(
                    database, recording_id, name="web-test", end_ms=5_000
                )
                server = create_web_server(
                    database_path=database_path,
                    config_path=root / "unused.toml",
                    port=0,
                    token="test-token",
                )
                with (
                    patch(
                        "allday_asr.web.recording_output_dir",
                        return_value=web_output,
                    ),
                    patch("allday_asr.web.extract_clip") as extract_clip,
                ):
                    listening_clip = server.application.audio_clip(segment_id)
                    context_clip = server.application.audio_clip(
                        segment_id, mode="context"
                    )
                self.assertEqual(
                    listening_clip.name, f"segment-{segment_id}-listening.wav"
                )
                self.assertEqual(
                    context_clip.name, f"segment-{segment_id}-context.wav"
                )
                self.assertEqual(extract_clip.call_count, 2)
                self.assertEqual(extract_clip.call_args_list[0].args[2:4], (500, 2_500))
                self.assertEqual(extract_clip.call_args_list[1].args[2:4], (0, 5_000))
                self.assertEqual(
                    extract_clip.call_args_list[0].kwargs["audio_filter"],
                    "loudnorm=I=-18:LRA=7:TP=-2",
                )
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base_url = server.application.base_url

                with self.assertRaises(urllib.error.HTTPError) as denied:
                    urllib.request.urlopen(f"{base_url}/api/recordings", timeout=3)
                self.assertEqual(denied.exception.code, 403)

                cookie_jar = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(
                    urllib.request.HTTPCookieProcessor(cookie_jar)
                )
                with opener.open(f"{base_url}/?token=test-token", timeout=3) as response:
                    html = response.read().decode("utf-8")
                self.assertIn("<title>AllDay · 本地日记工作台</title>", html)
                self.assertIn("说话人时间轴", html)
                with opener.open(f"{base_url}/assets/app.js", timeout=3) as response:
                    javascript = response.read().decode("utf-8")
                self.assertIn("DOMContentLoaded", javascript)
                self.assertIn("renderTimelineOverview", javascript)

                with opener.open(
                    f"{base_url}/api/dashboard?recording_id={recording_id}",
                    timeout=3,
                ) as response:
                    dashboard = json.load(response)
                self.assertEqual(dashboard["segments"]["completed"], 1)
                self.assertEqual(dashboard["evaluations"][0]["segments"], 1)

                with opener.open(
                    f"{base_url}/api/speaker-timeline?recording_id={recording_id}",
                    timeout=3,
                ) as response:
                    timeline = json.load(response)
                self.assertFalse(timeline["available"])

                with opener.open(
                    f"{base_url}/api/evaluations/{recording_id}/web-test",
                    timeout=3,
                ) as response:
                    evaluation = json.load(response)
                self.assertEqual(
                    evaluation["segments"][0]["audio_url"],
                    f"/api/audio/{segment_id}?v=3",
                )
                self.assertEqual(
                    evaluation["segments"][0]["context_audio_url"],
                    f"/api/audio/{segment_id}?mode=context&v=1",
                )

                request = urllib.request.Request(
                    f"{base_url}/api/evaluations/{recording_id}/web-test/segments/{segment_id}",
                    data=json.dumps(
                        {
                            "reference_text": "明天十点见",
                            "reference_speaker": "self",
                            "reference_identity": "self",
                            "key_facts": ["明天十点"],
                            "notes": "清晰",
                            "include": True,
                        }
                    ).encode("utf-8"),
                    method="PUT",
                    headers={
                        "Content-Type": "application/json",
                        "X-AllDay-Token": "test-token",
                    },
                )
                with urllib.request.urlopen(request, timeout=3) as response:
                    updated = json.load(response)["segment"]
                self.assertEqual(updated["reference_identity"], "self")
                self.assertEqual(updated["key_facts"], ["明天十点"])
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if evaluation_dir.exists():
                shutil.rmtree(evaluation_dir)
            if web_output.exists():
                shutil.rmtree(web_output)
            for database_suffix in ("", "-shm", "-wal"):
                candidate = Path(f"{database_path}{database_suffix}")
                if candidate.exists():
                    candidate.unlink()


if __name__ == "__main__":
    unittest.main()
