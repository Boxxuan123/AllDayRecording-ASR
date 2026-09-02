from __future__ import annotations

import json
import shutil
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from openai_codex import ApprovalMode, Sandbox
from openai_codex.types import ReasoningEffort

from allday_asr.v3.adapters.codex import (
    CODEX_SEMANTIC_EVENT_OUTPUT_SCHEMA,
    CodexSemanticEventGenerationError,
    CodexSemanticEventGenerator,
)
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import (
    KnowledgeArchitectureService,
    SemanticEventExtractionService,
    SemanticEventGenerationFailed,
)
from allday_asr.v3.application.event_extraction import (
    select_semantic_event_reasoning_effort,
)
from allday_asr.v3.domain import (
    Artifact,
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    Device,
    DeviceKind,
    DeviceStatus,
    ProcessingRun,
    ProcessingStatus,
    RecordingSession,
    RecordingSessionState,
    SelfIdentity,
    SpeakerTrack,
    Utterance,
)
from allday_asr.v3.ports.event_generation import (
    SemanticEventModelRequest,
    SemanticEventModelResult,
    SemanticEventReasoningEffort,
)


TEST_ROOT = Path(__file__).parent
SESSION_ID = "01990d5a-7c00-7000-8000-000000000201"
UTTERANCE_ID = "01990d5a-7c00-7000-8000-000000000202"
SPEAKER_ID = "01990d5a-7c00-7000-8000-000000000203"


class V32SemanticEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_ROOT / f"v32-semantic-events-{uuid4().hex}"
        self.database = V3Database.open(self.root / "core.sqlite3")
        self.current_time = datetime(2026, 8, 31, 16, 0, tzinfo=timezone.utc)
        self._seed()

        def factory() -> SqliteUnitOfWork:
            return SqliteUnitOfWork(self.database)

        self.factory = factory
        self.knowledge = KnowledgeArchitectureService(
            factory,
            now=lambda: self.current_time,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_high_confidence_record_event_is_accepted_and_idempotent(self) -> None:
        generator = _FakeSemanticEventGenerator((_decision(),))
        extraction = SemanticEventExtractionService(
            self.factory,
            self.knowledge,
            generator,
            allow_auto_accept=True,
        )

        first = extraction.extract(SESSION_ID, reasoning_effort="auto")

        self.assertEqual(first["auto_accepted_count"], 1)
        self.assertEqual(first["pending_review_count"], 0)
        self.assertEqual(first["semantic_events"][0]["status"], "accepted")
        events = self.knowledge.list_events(SESSION_ID)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_kind"], "decision")
        self.assertEqual(events[0]["payload"]["title"], "采用新的录音归档方案")
        self.assertEqual(
            events[0]["evidence"][0]["evidence_id"],
            UTTERANCE_ID,
        )
        history = self.knowledge.event_history(events[0]["event_id"])
        self.assertEqual(history[0]["actor"], "semantic-event-policy")
        with self.factory() as uow:
            insight_sources = uow.insights.event_sources()
        self.assertEqual(insight_sources[0]["event_id"], events[0]["event_id"])

        second = extraction.extract(SESSION_ID, reasoning_effort="medium")

        self.assertEqual(second["duplicate_count"], 1)
        self.assertEqual(second["semantic_events"], [])
        self.assertEqual(len(self.knowledge.list_events(SESSION_ID)), 1)

    def test_uncertain_event_stays_as_proposal_and_is_not_an_event(self) -> None:
        value = {**_decision(), "confidence": 0.72, "needs_confirmation": True}
        extraction = SemanticEventExtractionService(
            self.factory,
            self.knowledge,
            _FakeSemanticEventGenerator((value,)),
        )

        result = extraction.extract(SESSION_ID)

        self.assertEqual(result["auto_accepted_count"], 0)
        self.assertEqual(result["pending_review_count"], 1)
        self.assertEqual(self.knowledge.list_events(SESSION_ID), ())
        proposals = self.knowledge.list_proposals(status="pending")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["payload"]["event_kind"], "decision")

    def test_evidence_outside_frozen_request_fails_closed(self) -> None:
        value = {
            **_decision(),
            "evidence_utterance_ids": ["utterance-not-in-request"],
        }
        extraction = SemanticEventExtractionService(
            self.factory,
            self.knowledge,
            _FakeSemanticEventGenerator((value,)),
        )

        with self.assertRaisesRegex(
            SemanticEventGenerationFailed,
            "failed validation",
        ):
            extraction.extract(SESSION_ID)

        self.assertEqual(self.knowledge.list_events(SESSION_ID), ())
        self.assertEqual(self.knowledge.list_proposals(), ())

    def test_auto_reasoning_effort_scales_with_transcript_size(self) -> None:
        request = SemanticEventModelRequest(
            session_id="session",
            captured_timezone="Asia/Singapore",
            utterances=(_request_utterance(),),
        )
        self.assertEqual(
            select_semantic_event_reasoning_effort(request, "auto"),
            SemanticEventReasoningEffort.LOW,
        )
        large = SemanticEventModelRequest(
            session_id="session",
            captured_timezone="UTC",
            utterances=tuple(_request_utterance(index) for index in range(121)),
        )
        self.assertEqual(
            select_semantic_event_reasoning_effort(large, "auto"),
            SemanticEventReasoningEffort.HIGH,
        )

    def _seed(self) -> None:
        now = self.current_time
        with SqliteUnitOfWork(self.database) as uow:
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
            uow.catalog.add_session(
                RecordingSession(
                    session_id=SESSION_ID,
                    captured_start=now,
                    captured_end=now + timedelta(seconds=1),
                    timezone="Asia/Singapore",
                    state=RecordingSessionState.READY_FOR_PROCESSING,
                    revision=1,
                    status_code="ready",
                    current_stage=None,
                    progress=1.0,
                    blocking_reason=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            uow.catalog.add_asset(
                AudioAsset(
                    asset_id="asset-1",
                    sha256="a" * 64,
                    size_bytes=100,
                    duration_ms=1000,
                    format=AudioFormat.WAV,
                    media_id="media-1",
                    created_at=now,
                )
            )
            uow.catalog.add_replica(
                AudioReplica(
                    replica_id="replica-1",
                    asset_id="asset-1",
                    device_id="computer-1",
                    storage_key="sha256/aa/test.wav",
                    state=AudioReplicaState.AVAILABLE,
                    verified_at=now,
                    created_at=now,
                )
            )
            uow.catalog.add_segment(
                CaptureSegment(
                    segment_id="segment-1",
                    session_id=SESSION_ID,
                    asset_id="asset-1",
                    replica_id="replica-1",
                    sequence=0,
                    session_start_ms=0,
                    session_end_ms=1000,
                    source_start_ms=0,
                    source_end_ms=1000,
                    start_sample=0,
                    captured_at=now,
                )
            )
            uow.processing_runs.add(
                ProcessingRun(
                    run_id="run-1",
                    session_id=SESSION_ID,
                    pipeline_version="v3.2-test",
                    input_revision=1,
                    status=ProcessingStatus.SUCCEEDED,
                    config_digest="config",
                    current_stage=None,
                    progress=1.0,
                    created_at=now,
                    updated_at=now,
                    completed_at=now,
                )
            )
            uow.artifacts.add(
                Artifact(
                    artifact_id="artifact-1",
                    run_id="run-1",
                    kind="utterance_projection",
                    producer="test",
                    producer_version="1",
                    config_digest="config",
                    input_refs=(),
                    storage_ref="sha256/bb/test.json",
                    status="active",
                    created_at=now,
                )
            )
            uow.evidence.add_speaker_track(
                SpeakerTrack(
                    speaker_track_id=SPEAKER_ID,
                    session_id=SESSION_ID,
                    run_id="run-1",
                    label="SPEAKER_00",
                    source_artifact_id="artifact-1",
                    created_at=now,
                )
            )
            uow.evidence.add_utterance(
                Utterance(
                    utterance_id=UTTERANCE_ID,
                    session_id=SESSION_ID,
                    run_id="run-1",
                    source_artifact_id="artifact-1",
                    speaker_track_id=SPEAKER_ID,
                    original_speaker_track_id=SPEAKER_ID,
                    ordinal=0,
                    start_ms=100,
                    end_ms=900,
                    start_at=now + timedelta(milliseconds=100),
                    end_at=now + timedelta(milliseconds=900),
                    text="我们决定采用新的录音归档方案。",
                    original_text="我们决定采用新的录音归档方案。",
                    identity=SelfIdentity.SELF,
                    original_identity=SelfIdentity.SELF,
                    identity_evidence={"source": "test", "decision": "self"},
                    evidence={"asset_id": "asset-1"},
                    revision=1,
                    status="active",
                    created_at=now,
                    updated_at=now,
                )
            )


class V32CodexSemanticEventAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workdir = TEST_ROOT / f"codex-event-empty-{uuid4().hex}"

    def tearDown(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)

    def test_codex_event_extraction_is_ephemeral_read_only_and_tool_free(self) -> None:
        fake = _FakeCodex({"events": []})
        generator = CodexSemanticEventGenerator(
            self.workdir,
            model="codex-test-model",
            codex_factory=lambda: fake,
        )

        result = generator.generate(
            SemanticEventModelRequest(
                session_id="session",
                captured_timezone="UTC",
                utterances=(_request_utterance(),),
            ),
            SemanticEventReasoningEffort.MEDIUM,
        )

        self.assertEqual(result.events, ())
        self.assertEqual(fake.thread_kwargs["sandbox"], Sandbox.read_only)
        self.assertEqual(fake.thread_kwargs["approval_mode"], ApprovalMode.deny_all)
        self.assertTrue(fake.thread_kwargs["ephemeral"])
        self.assertEqual(fake.run_kwargs["effort"], ReasoningEffort.medium)
        self.assertEqual(
            fake.run_kwargs["output_schema"],
            CODEX_SEMANTIC_EVENT_OUTPUT_SCHEMA,
        )
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_codex_event_tool_activity_is_rejected(self) -> None:
        fake = _FakeCodex({"events": []}, item_type="commandExecution")
        generator = CodexSemanticEventGenerator(
            self.workdir,
            codex_factory=lambda: fake,
        )

        with self.assertRaisesRegex(
            CodexSemanticEventGenerationError,
            "tool or unknown activity",
        ):
            generator.generate(
                SemanticEventModelRequest(
                    session_id="session",
                    captured_timezone="UTC",
                    utterances=(_request_utterance(),),
                ),
                SemanticEventReasoningEffort.LOW,
            )


class _FakeSemanticEventGenerator:
    model_label = "codex-test"
    producer_version = "0.147.0"
    prompt_version = "prompt-test"
    extractor_version = "extractor-test"

    def __init__(self, events: tuple[dict[str, object], ...]) -> None:
        self.events = events
        self.requests: list[SemanticEventModelRequest] = []

    def generate(
        self,
        request: SemanticEventModelRequest,
        effort: SemanticEventReasoningEffort,
    ) -> SemanticEventModelResult:
        self.requests.append(request)
        return SemanticEventModelResult(
            events=self.events,
            turn_id="codex-turn-test",
            reasoning_effort=effort,
            usage={"total_tokens": 123},
        )

    def close(self) -> None:
        pass


class _FakeThread:
    def __init__(self, owner: _FakeCodex) -> None:
        self.owner = owner

    def run(self, prompt: str, **kwargs):
        self.owner.prompt = prompt
        self.owner.run_kwargs = kwargs
        items = []
        if self.owner.item_type is not None:
            items.append(
                SimpleNamespace(root=SimpleNamespace(type=self.owner.item_type))
            )
        return SimpleNamespace(
            id="turn-test",
            final_response=json.dumps(self.owner.response),
            items=items,
            usage=SimpleNamespace(model_dump=lambda **_kwargs: {"total_tokens": 42}),
        )


class _FakeCodex:
    def __init__(self, response: dict[str, object], item_type: str | None = None):
        self.response = response
        self.item_type = item_type
        self.thread_kwargs: dict[str, object] = {}
        self.run_kwargs: dict[str, object] = {}
        self.prompt = ""

    def thread_start(self, **kwargs) -> _FakeThread:
        self.thread_kwargs = kwargs
        return _FakeThread(self)

    def close(self) -> None:
        pass


def _decision() -> dict[str, object]:
    return {
        "event_kind": "decision",
        "title": "采用新的录音归档方案",
        "summary": "对话明确决定采用新的录音归档方案。",
        "topics": ["录音归档"],
        "related_person_ids": [],
        "person_id": None,
        "confidence": 0.96,
        "evidence_utterance_ids": [UTTERANCE_ID],
        "needs_confirmation": False,
        "reason": "说话者明确使用了决定性表述",
    }


def _request_utterance(index: int = 0) -> dict[str, object]:
    return {
        "utterance_id": f"utterance-{index}",
        "start_at": "2026-09-01T00:00:00Z",
        "end_at": "2026-09-01T00:00:01Z",
        "speaker_label": "SPEAKER_00",
        "speaker_person_id": None,
        "speaker_name": None,
        "identity": "self",
        "text": "我们决定采用新的归档方案。",
        "revision": 1,
    }


if __name__ == "__main__":
    unittest.main()
