from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort

from allday_asr.v3.adapters.codex import CodexInsightGenerator
from allday_asr.v3.adapters.codex.insight_generator import (
    CODEX_DAILY_INSIGHT_OUTPUT_SCHEMA,
)
from allday_asr.v3.domain.insights import DAILY_NARRATIVE_SECTIONS
from allday_asr.v3.ports.insight_generation import (
    DailyInsightModelRequest,
    InsightReasoningEffort,
)


TEST_ROOT = Path(__file__).parent


class V36CodexInsightAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = TEST_ROOT / f"codex-insight-empty-{uuid4().hex}"

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_daily_synthesis_is_empty_read_only_ephemeral_and_structured(self) -> None:
        response = {section: [] for section in DAILY_NARRATIVE_SECTIONS}
        fake = _FakeCodex(response)
        generator = CodexInsightGenerator(
            self.workdir,
            model="codex-test-model",
            codex_factory=lambda: fake,
        )

        result = generator.generate_daily(
            DailyInsightModelRequest(
                summary_date="2026-09-01",
                timezone="Asia/Singapore",
                objective={"statistics": {"event_count": 0}},
                source_events=(),
                key_quotes=(),
            ),
            InsightReasoningEffort.HIGH,
        )

        self.assertEqual(result.narrative["what_happened"], ())
        self.assertEqual(fake.thread_kwargs["cwd"], str(self.workdir.resolve()))
        self.assertEqual(fake.thread_kwargs["sandbox"], Sandbox.read_only)
        self.assertEqual(fake.thread_kwargs["approval_mode"], ApprovalMode.deny_all)
        self.assertTrue(fake.thread_kwargs["ephemeral"])
        self.assertEqual(fake.run_kwargs["sandbox"], Sandbox.read_only)
        self.assertEqual(fake.run_kwargs["approval_mode"], ApprovalMode.deny_all)
        self.assertEqual(fake.run_kwargs["effort"], ReasoningEffort.high)
        self.assertEqual(
            fake.run_kwargs["output_schema"], CODEX_DAILY_INSIGHT_OUTPUT_SCHEMA
        )
        self.assertNotIn("PycharmProjects", fake.prompt)
        self.assertEqual(list(self.workdir.iterdir()), [])
        generator.close()
        self.assertTrue(fake.closed)


class _FakeThread:
    def __init__(self, owner) -> None:
        self.owner = owner

    def run(self, prompt: str, **kwargs):
        self.owner.prompt = prompt
        self.owner.run_kwargs = kwargs
        return SimpleNamespace(
            id="turn-insight",
            final_response=json.dumps(self.owner.response),
            items=[],
            usage=None,
        )


class _FakeCodex:
    def __init__(self, response) -> None:
        self.response = response
        self.thread_kwargs = {}
        self.run_kwargs = {}
        self.prompt = ""
        self.closed = False

    def thread_start(self, **kwargs):
        self.thread_kwargs = kwargs
        return _FakeThread(self)

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
