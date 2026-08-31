from __future__ import annotations

import hashlib
import json
import shutil
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.interfaces.transfer.store import UploadStore
from allday_asr.interfaces.transfer.workflow import AutomaticWorkflowRunner


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class AutomaticTransferWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(__file__).parent / f"transfer-workflow-{uuid4().hex}"
        self.workspace.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)

    def test_completed_manifest_runs_once_and_persists_result(self) -> None:
        inbox = self.workspace / "inbox"
        manifest = b'{"format":"test"}'
        store = UploadStore(inbox, max_chunk_bytes=64)
        upload, _ = store.create_upload(
            relative_path="pcm_session_1234567890123/session_summary.json",
            size=len(manifest),
            sha256=_digest(manifest),
            kind="manifest",
        )
        completed = store.append_chunk(
            upload.upload_id,
            offset=0,
            data=manifest,
        )
        calls: list[Path] = []

        def process(path: Path, progress):
            calls.append(path)
            progress("asr", "running")
            return {
                "session_id": 7,
                "workflow_run_id": 11,
                "workflow_state": "semantic_ready",
            }

        runner = AutomaticWorkflowRunner(inbox, processor=process)
        queued = runner.submit(completed)
        self.assertIsNotNone(queued)
        runner.submit(completed)
        runner.close()

        self.assertEqual(calls, [inbox / completed.relative_path])
        status = runner.get_status(completed.upload_id)
        self.assertIsNotNone(status)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["result"]["workflow_run_id"], 11)

        restarted_calls: list[Path] = []
        restarted = AutomaticWorkflowRunner(
            inbox,
            processor=lambda path, progress: restarted_calls.append(path) or {},
        )
        recovered = restarted.submit(completed)
        restarted.close()
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(restarted_calls, [])

        changed_calls: list[Path] = []
        changed = AutomaticWorkflowRunner(
            inbox,
            processor=lambda path, progress: changed_calls.append(path)
            or {
                "session_id": 7,
                "workflow_run_id": 13,
                "workflow_state": "semantic_ready",
            },
            configuration={"admission_mode": "production"},
        )
        changed.submit(completed)
        changed.close()
        self.assertEqual(changed_calls, [inbox / completed.relative_path])
        self.assertEqual(
            changed.get_status(completed.upload_id)["result"]["workflow_run_id"],
            13,
        )

    def test_stale_running_state_is_requeued_after_restart(self) -> None:
        inbox = self.workspace / "inbox"
        manifest = b"{}"
        store = UploadStore(inbox, max_chunk_bytes=64)
        upload, _ = store.create_upload(
            relative_path="pcm_session_1234567890124/session_summary.json",
            size=len(manifest),
            sha256=_digest(manifest),
            kind="manifest",
        )
        completed = store.append_chunk(upload.upload_id, offset=0, data=manifest)
        stale = {
            "version": 1,
            "upload_id": completed.upload_id,
            "relative_path": completed.relative_path,
            "manifest_sha256": completed.sha256,
            "status": "running",
            "stage": "asr",
            "detail": "process stopped",
            "updated_at": "2026-08-31T00:00:00+00:00",
        }
        state_path = inbox / ".uploads" / f"{completed.upload_id}.workflow.json"
        state_path.write_text(json.dumps(stale), encoding="utf-8")
        calls: list[Path] = []
        runner = AutomaticWorkflowRunner(
            inbox,
            processor=lambda path, progress: calls.append(path)
            or {
                "session_id": 8,
                "workflow_run_id": 12,
                "workflow_state": "semantic_ready_empty",
            },
        )

        runner.submit(completed)
        runner.close()

        self.assertEqual(calls, [inbox / completed.relative_path])
        self.assertEqual(runner.get_status(completed.upload_id)["status"], "completed")

    def test_recording_completion_does_not_start_workflow(self) -> None:
        inbox = self.workspace / "inbox"
        content = b"audio"
        store = UploadStore(inbox, max_chunk_bytes=64)
        upload, _ = store.create_upload(
            relative_path="pcm_session_1234567890125/segment_000.wav",
            size=len(content),
            sha256=_digest(content),
            kind="recording",
        )
        completed = store.append_chunk(upload.upload_id, offset=0, data=content)
        calls: list[Path] = []
        runner = AutomaticWorkflowRunner(
            inbox,
            processor=lambda path, progress: calls.append(path) or {},
        )

        self.assertIsNone(runner.submit(completed))
        runner.close()
        self.assertEqual(calls, [])

    def test_failed_task_can_resume_on_receiver_restart(self) -> None:
        inbox = self.workspace / "inbox"
        manifest = b"{}"
        store = UploadStore(inbox, max_chunk_bytes=64)
        upload, _ = store.create_upload(
            relative_path="pcm_session_1234567890126/session_summary.json",
            size=len(manifest),
            sha256=_digest(manifest),
            kind="manifest",
        )
        completed = store.append_chunk(upload.upload_id, offset=0, data=manifest)

        def fail(path: Path, progress):
            raise RuntimeError("synthetic model failure")

        first = AutomaticWorkflowRunner(inbox, processor=fail)
        first.submit(completed)
        first.close()
        self.assertEqual(first.get_status(completed.upload_id)["status"], "failed")

        calls: list[Path] = []
        restarted = AutomaticWorkflowRunner(
            inbox,
            processor=lambda path, progress: calls.append(path)
            or {
                "session_id": 9,
                "workflow_run_id": 14,
                "workflow_state": "semantic_ready",
            },
        )
        restarted.submit(completed)
        restarted.close()
        self.assertEqual(calls, [inbox / completed.relative_path])
        self.assertEqual(
            restarted.get_status(completed.upload_id)["status"],
            "completed",
        )


if __name__ == "__main__":
    unittest.main()
