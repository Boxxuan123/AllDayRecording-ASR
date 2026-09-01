from __future__ import annotations

import io
import shutil
import sqlite3
import unittest
import wave
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application.knowledge import KnowledgeArchitectureService
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
from allday_asr.v3.config import CodexReminderSettings
from allday_asr.v3.domain.knowledge import (
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.domain.people import (
    RepresentativeClip,
    SpeakerEmbedding,
    conservative_match,
)
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


TEST_STATE = Path(__file__).parents[1] / "state"
NOW = datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat()


class FakeEmbeddingProvider:
    model = "fixture-speaker"
    model_version = "1"

    def __init__(self, vectors: dict[str, tuple[float, ...]]) -> None:
        self.vectors = vectors

    def embed(self, tracks):
        return tuple(
            SpeakerEmbedding(
                speaker_track_id=track.speaker_track_id,
                model=self.model,
                model_version=self.model_version,
                vector=self.vectors[track.speaker_track_id],
                representatives=(
                    RepresentativeClip(
                        track.clips[0].media_id,
                        track.clips[0].source_start_ms,
                        track.clips[0].source_end_ms,
                        track.clips[0].utterance_id,
                    ),
                ),
                quality_score=0.95,
            )
            for track in tracks
        )


class FakeCAMBackend:
    def extract_speaker_embeddings(self, samples, *, batch_size=8):
        return np.asarray([[3.0, 4.0] for _ in samples], dtype=np.float32)


class V34OpenSpeakerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_STATE / f"v34-people-{uuid4().hex}"
        self.paths = V3CorePaths.from_state_dir(self.root)
        self.provider = FakeEmbeddingProvider(
            {
                "track-1": (1.0, 0.0, 0.0),
                "track-2": (0.999, 0.02, 0.0),
                "track-3": (0.998, 0.03, 0.0),
                "track-4": (0.0, 1.0, 0.0),
            }
        )
        self.core = compose_v3_core(
            self.paths,
            codex_settings=CodexReminderSettings(enabled=False),
            speaker_embedding_provider=self.provider,
        )
        self.assertEqual(self.core.initialize(), 9)
        with self.core.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO devices (
                  device_id, kind, name, status, revision, created_at, updated_at
                ) VALUES ('device-1', 'computer', 'fixture', 'active', 1, ?, ?)
                """,
                (NOW_TEXT, NOW_TEXT),
            )

    def tearDown(self) -> None:
        self.core.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_unknown_clusters_cross_session_then_known_match_remains_a_suggestion(self) -> None:
        self._seed_track(1)
        first = self.core.people.analyze("session-1")
        self.assertEqual(first["new_cluster_count"], 1)
        cluster_id = self.core.people.list_clusters()[0]["cluster_id"]

        self._seed_track(2)
        second = self.core.people.analyze("session-2")
        self.assertEqual(second["matched_track_count"], 1)
        cluster = self.core.people.cluster(cluster_id)
        self.assertEqual(cluster["session_count"], 2)
        self.assertEqual(cluster["track_count"], 2)

        person = self.core.people.create_person("张老师")
        labeled = self.core.people.label_cluster(cluster_id, person["person_id"])
        self.assertEqual(labeled["person_id"], person["person_id"])
        self.assertEqual(
            self.core.people.list_people()[0]["prototype_count"], 2
        )

        self._seed_track(3)
        third = self.core.people.analyze("session-3")
        self.assertEqual(third["new_cluster_count"], 1)
        self.assertEqual(third["person_suggestion_count"], 1)
        new_cluster = next(
            value for value in self.core.people.list_clusters()
            if value["cluster_id"] != cluster_id
        )
        self.assertIsNone(new_cluster["person_id"])
        self.assertEqual(new_cluster["suggested_person_id"], person["person_id"])

    def test_label_rebinds_event_reference_and_keeps_event_history(self) -> None:
        self._seed_track(1)
        self.core.people.analyze("session-1")
        cluster_id = self.core.people.list_clusters()[0]["cluster_id"]
        knowledge = KnowledgeArchitectureService(
            lambda: SqliteUnitOfWork(self.core.database), now=lambda: NOW
        )
        generation = knowledge.submit_generation(
            GenerationSubmission(
                layer=KnowledgeLayer.EVENT,
                producer="fixture",
                producer_version="1",
                model="none",
                prompt_version="1",
                extractor_version="1",
                input_scope={"session_id": "session-1"},
                proposals=(
                    (
                        ProposalKind.EVENT_OPERATION,
                        {
                            "operation": "create",
                            "session_id": "session-1",
                            "event_kind": "task",
                            "expected_revision": 0,
                            "patch": {
                                "title": "回电话",
                                "actor_person_id": cluster_id,
                                "related_person_ids": [cluster_id],
                            },
                        },
                        ("utterance-1",),
                    ),
                ),
            )
        )
        knowledge.accept_proposal(generation["proposals"][0]["proposal_id"], "fixture")
        person = self.core.people.create_person("王同学")

        result = self.core.people.label_cluster(cluster_id, person["person_id"])

        self.assertEqual(len(result["rebound_event_ids"]), 1)
        event = knowledge.list_events("session-1")[0]
        self.assertEqual(event["payload"]["actor_person_id"], person["person_id"])
        self.assertEqual(event["payload"]["related_person_ids"], [person["person_id"]])
        self.assertEqual(event["revision"], 2)
        self.assertEqual(len(knowledge.event_history(event["event_id"])), 2)

        undone = self.core.people.undo(cluster_id)
        self.assertEqual(undone["rebound_event_ids"], [event["event_id"]])
        restored = knowledge.list_events("session-1")[0]
        self.assertEqual(restored["payload"]["actor_person_id"], cluster_id)
        self.assertEqual(restored["revision"], 3)

    def test_split_ignore_and_undo_are_reversible_without_mutating_prototypes(self) -> None:
        self._seed_track(1)
        self._seed_track(2)
        self.core.people.analyze("session-1")
        self.core.people.analyze("session-2")
        cluster = self.core.people.list_clusters()[0]
        detail = self.core.people.cluster(cluster["cluster_id"])

        self.core.people.split(
            cluster["cluster_id"], (detail["members"][0]["speaker_track_id"],)
        )
        self.assertEqual(len(self.core.people.list_clusters("active")), 2)
        self.core.people.undo(cluster["cluster_id"])
        self.assertEqual(len(self.core.people.list_clusters("active")), 1)
        self.core.people.ignore(cluster["cluster_id"], "television")
        self.assertEqual(self.core.people.cluster(cluster["cluster_id"])["status"], "ignored")
        self.core.people.undo(cluster["cluster_id"])
        self.assertEqual(self.core.people.cluster(cluster["cluster_id"])["status"], "active")

        with self.core.database.transaction() as connection:
            prototype_id = connection.execute(
                "SELECT prototype_id FROM voice_prototypes LIMIT 1"
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE voice_prototypes SET quality_score = 0 WHERE prototype_id = ?",
                    (prototype_id,),
                )

    def test_conservative_match_rejects_ambiguous_or_below_threshold_candidates(self) -> None:
        ambiguous = conservative_match(
            (1.0, 0.0),
            (("a", (1.0, 0.0)), ("b", (0.999, 0.01))),
            threshold=0.82,
            minimum_margin=0.05,
        )
        below = conservative_match(
            (1.0, 0.0),
            (("a", (0.0, 1.0)),),
            threshold=0.82,
            minimum_margin=0.05,
        )
        self.assertIsNone(ambiguous.target_id)
        self.assertEqual(ambiguous.reason, "ambiguous")
        self.assertIsNone(below.target_id)
        self.assertEqual(below.reason, "below_threshold")

    def test_local_adapter_reads_content_store_and_keeps_paths_off_the_result(self) -> None:
        store = ContentAddressedStore(self.root / "adapter-audio")
        stored = store.put_bytes(_wav_bytes())
        provider = FunASRSpeakerEmbeddingProvider(
            store,
            backend_factory=lambda: FakeCAMBackend(),
            temp_root=self.root / "adapter-temp",
        )
        request = SpeakerTrackInput(
            speaker_track_id="track-adapter",
            session_id="session-adapter",
            clips=(
                SpeakerClipInput(
                    media_id=stored.media_id,
                    storage_key=stored.storage_key,
                    source_start_ms=0,
                    source_end_ms=1_000,
                    utterance_id=None,
                ),
            ),
        )

        def copy_clip(source, destination, start_ms, end_ms):
            destination.write_bytes(source.read_bytes())
            return destination

        manual_temp = self.root / "adapter-temp" / "manual"
        manual_temp.mkdir(parents=True)

        @contextmanager
        def static_temp(**kwargs):
            yield str(manual_temp)

        with patch(
            "allday_asr.v3.adapters.speaker_embeddings.funasr.extract_clip",
            side_effect=copy_clip,
        ), patch(
            "allday_asr.v3.adapters.speaker_embeddings.funasr.tempfile.TemporaryDirectory",
            side_effect=static_temp,
        ):
            result = provider.embed((request,))[0]

        self.assertAlmostEqual(result.vector[0], 0.6)
        self.assertAlmostEqual(result.vector[1], 0.8)
        self.assertEqual(result.representatives[0].media_id, stored.media_id)
        self.assertNotIn(str(self.root), repr(result))

    def _seed_track(self, number: int) -> None:
        session_id = f"session-{number}"
        run_id = f"run-{number}"
        artifact_id = f"artifact-{number}"
        asset_id = f"asset-{number}"
        replica_id = f"replica-{number}"
        track_id = f"track-{number}"
        with self.core.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO recording_sessions (
                  session_id, captured_start, captured_end, timezone, state,
                  revision, status_code, progress, created_at, updated_at
                ) VALUES (?, ?, ?, 'UTC', 'ready_for_processing', 1,
                  'available', 1, ?, ?)
                """,
                (session_id, NOW_TEXT, NOW_TEXT, NOW_TEXT, NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO audio_assets (
                  asset_id, sha256, size_bytes, duration_ms, format, media_id, created_at
                ) VALUES (?, ?, 320000, 10000, 'wav', ?, ?)
                """,
                (asset_id, f"{number:064x}", f"media-{number}", NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO audio_replicas (
                  replica_id, asset_id, device_id, storage_key, state, verified_at, created_at
                ) VALUES (?, ?, 'device-1', ?, 'available', ?, ?)
                """,
                (replica_id, asset_id, f"fixture/{number}", NOW_TEXT, NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO capture_segments (
                  segment_id, session_id, asset_id, replica_id, sequence,
                  session_start_ms, session_end_ms, source_start_ms, source_end_ms,
                  captured_at, created_at
                ) VALUES (?, ?, ?, ?, 0, 0, 10000, 0, 10000, ?, ?)
                """,
                (f"segment-{number}", session_id, asset_id, replica_id, NOW_TEXT, NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO processing_runs (
                  run_id, session_id, pipeline_version, input_revision, status,
                  config_digest, current_stage, progress, completed_at, created_at, updated_at
                ) VALUES (?, ?, 'fixture', 1, 'succeeded', ?, 'complete', 1, ?, ?, ?)
                """,
                (run_id, session_id, "a" * 64, NOW_TEXT, NOW_TEXT, NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO artifacts (
                  artifact_id, run_id, kind, producer, producer_version,
                  config_digest, input_refs_json, storage_ref, sha256, size_bytes,
                  status, metadata_json, created_at
                ) VALUES (?, ?, 'diarization', 'fixture', '1', ?, '[]', ?, ?, 1,
                  'active', '{}', ?)
                """,
                (
                    artifact_id,
                    run_id,
                    "a" * 64,
                    f"artifact/{number}",
                    f"{number + 100:064x}",
                    NOW_TEXT,
                ),
            )
            connection.execute(
                """
                INSERT INTO speaker_tracks (
                  speaker_track_id, session_id, run_id, label, source_artifact_id, created_at
                ) VALUES (?, ?, ?, 'speaker_01', ?, ?)
                """,
                (track_id, session_id, run_id, artifact_id, NOW_TEXT),
            )
            connection.execute(
                """
                INSERT INTO utterances (
                  utterance_id, session_id, run_id, source_artifact_id,
                  speaker_track_id, original_speaker_track_id, ordinal,
                  start_ms, end_ms, start_at, end_at, text, original_text,
                  identity, original_identity, identity_evidence_json, evidence_json,
                  revision, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1000, 6000, ?, ?, '测试声音',
                  '测试声音', 'unknown', 'unknown', ?, '{}', 1, 'active', ?, ?)
                """,
                (
                    f"utterance-{number}",
                    session_id,
                    run_id,
                    artifact_id,
                    track_id,
                    track_id,
                    NOW_TEXT,
                    NOW_TEXT,
                    '{"source":"none","decision":"unknown"}',
                    NOW_TEXT,
                    NOW_TEXT,
                ),
            )


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x01" * 16_000)
    return output.getvalue()


if __name__ == "__main__":
    unittest.main()
