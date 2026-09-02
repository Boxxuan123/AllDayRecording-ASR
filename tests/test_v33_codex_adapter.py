from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort

from allday_asr.v3.adapters.codex import (
    CODEX_REMINDER_OUTPUT_SCHEMA,
    CodexReminderGenerationError,
    CodexReminderGenerator,
)
from allday_asr.v3.application.reminder_extraction import (
    select_reasoning_effort,
)
from allday_asr.v3.config import (
    CodexEffortSetting,
    CodexReminderSettings,
    V3ConfigurationError,
)
from allday_asr.v3.ports.reminder_generation import (
    ReminderModelRequest,
    ReminderReasoningEffort,
)


TEST_ROOT = Path(__file__).parent


class V33CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = TEST_ROOT / f"codex-empty-{uuid4().hex}"

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_codex_runs_in_empty_read_only_ephemeral_thread_with_no_approvals(
        self,
    ) -> None:
        fake = _FakeCodex({"intents": []})
        generator = CodexReminderGenerator(
            self.workdir,
            model="codex-test-model",
            codex_factory=lambda: fake,
        )

        result = generator.generate(_request(), ReminderReasoningEffort.MEDIUM)

        self.assertEqual(result.intents, ())
        self.assertEqual(result.reasoning_effort, ReminderReasoningEffort.MEDIUM)
        self.assertEqual(fake.thread_kwargs["cwd"], str(self.workdir.resolve()))
        self.assertEqual(fake.thread_kwargs["sandbox"], Sandbox.read_only)
        self.assertEqual(fake.thread_kwargs["approval_mode"], ApprovalMode.deny_all)
        self.assertTrue(fake.thread_kwargs["ephemeral"])
        self.assertEqual(fake.run_kwargs["sandbox"], Sandbox.read_only)
        self.assertEqual(fake.run_kwargs["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(fake.run_kwargs["effort"], ReasoningEffort.medium)
        self.assertEqual(fake.run_kwargs["output_schema"], CODEX_REMINDER_OUTPUT_SCHEMA)
        self.assertNotIn("PycharmProjects", fake.prompt)
        self.assertIn("direct_command", fake.prompt)
        self.assertEqual(list(self.workdir.iterdir()), [])
        generator.close()
        self.assertTrue(fake.closed)

    def test_codex_tool_activity_is_rejected_even_in_read_only_mode(self) -> None:
        fake = _FakeCodex({"intents": []}, item_type="commandExecution")
        generator = CodexReminderGenerator(
            self.workdir,
            codex_factory=lambda: fake,
        )

        with self.assertRaisesRegex(
            CodexReminderGenerationError, "tool or unknown activity"
        ):
            generator.generate(_request(), ReminderReasoningEffort.LOW)

        future_activity = _FakeCodex(
            {"intents": []}, item_type="futureUnknownTool"
        )
        generator = CodexReminderGenerator(
            self.workdir,
            codex_factory=lambda: future_activity,
        )
        with self.assertRaisesRegex(
            CodexReminderGenerationError, "tool or unknown activity"
        ):
            generator.generate(_request(), ReminderReasoningEffort.LOW)

    def test_auto_effort_uses_low_medium_and_high_but_never_xhigh(self) -> None:
        self.assertEqual(
            select_reasoning_effort(_request(), "auto"),
            ReminderReasoningEffort.LOW,
        )
        medium = _request(
            utterances=tuple(
                _utterance(f"utterance-{index}", "普通对话") for index in range(20)
            )
        )
        self.assertEqual(
            select_reasoning_effort(medium, "auto"),
            ReminderReasoningEffort.MEDIUM,
        )
        high = _request(
            utterances=(_utterance("utterance-1", "刚才说错了，改到十一点"),),
            active_reminders=(_active_reminder(),),
        )
        self.assertEqual(
            select_reasoning_effort(high, "auto"),
            ReminderReasoningEffort.HIGH,
        )
        self.assertEqual(
            select_reasoning_effort(high, "xhigh"),
            ReminderReasoningEffort.XHIGH,
        )

    def test_codex_environment_settings_are_explicit_and_fail_closed(self) -> None:
        defaults = CodexReminderSettings.from_environment(
            {"ALLDAY_V3_CODEX_WORKDIR": str(self.workdir)}
        )
        self.assertTrue(defaults.allow_auto_apply)
        settings = CodexReminderSettings.from_environment(
            {
                "ALLDAY_V3_CODEX_ENABLED": "1",
                "ALLDAY_V3_CODEX_WORKDIR": str(self.workdir),
                "ALLDAY_V3_CODEX_REASONING_EFFORT": "high",
                "ALLDAY_V3_CODEX_AUTO_APPLY": "0",
            }
        )
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.reasoning_effort, CodexEffortSetting.HIGH)
        self.assertFalse(settings.allow_auto_apply)
        with self.assertRaises(V3ConfigurationError):
            CodexReminderSettings.from_environment(
                {"ALLDAY_V3_CODEX_REASONING_EFFORT": "maximum"}
            )
        with self.assertRaises(V3ConfigurationError):
            CodexReminderSettings.from_environment(
                {"ALLDAY_V3_CODEX_WORKDIR": "  "}
            )


class _FakeUsage:
    def model_dump(self, **_kwargs) -> dict[str, int]:
        return {"total_tokens": 42}


class _FakeThread:
    def __init__(self, owner: _FakeCodex) -> None:
        self.owner = owner

    def run(self, prompt: str, **kwargs):
        self.owner.prompt = prompt
        self.owner.run_kwargs = kwargs
        items = []
        if self.owner.item_type is not None:
            items.append(
                SimpleNamespace(
                    root=SimpleNamespace(type=self.owner.item_type)
                )
            )
        return SimpleNamespace(
            id="turn-test",
            final_response=json.dumps(self.owner.response),
            items=items,
            usage=_FakeUsage(),
        )


class _FakeCodex:
    def __init__(self, response: dict[str, object], item_type: str | None = None):
        self.response = response
        self.item_type = item_type
        self.thread_kwargs: dict[str, object] = {}
        self.run_kwargs: dict[str, object] = {}
        self.prompt = ""
        self.closed = False

    def thread_start(self, **kwargs) -> _FakeThread:
        self.thread_kwargs = kwargs
        return _FakeThread(self)

    def close(self) -> None:
        self.closed = True


def _request(
    *,
    utterances: tuple[dict[str, object], ...] | None = None,
    active_reminders: tuple[dict[str, object], ...] = (),
) -> ReminderModelRequest:
    return ReminderModelRequest(
        session_id="session-test",
        captured_timezone="Asia/Singapore",
        now_utc="2026-09-01T00:00:00Z",
        utterances=utterances or (_utterance("utterance-1", "明天十点提醒我交材料"),),
        active_reminders=active_reminders,
    )


def _utterance(utterance_id: str, text: str) -> dict[str, object]:
    return {
        "utterance_id": utterance_id,
        "start_at": "2026-09-01T00:00:00Z",
        "end_at": "2026-09-01T00:00:01Z",
        "speaker_label": "SPEAKER_00",
        "identity": "self",
        "text": text,
        "revision": 1,
    }


def _active_reminder() -> dict[str, object]:
    return {
        "event_id": "event-1",
        "event_revision": 1,
        "title": "见面",
        "actor_person_id": "self",
        "commitment_direction": "mutual",
        "related_person_ids": [],
        "scheduled_at": "2026-09-02T02:00:00Z",
        "location": None,
        "status": "scheduled",
    }


if __name__ == "__main__":
    unittest.main()
