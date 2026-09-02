from __future__ import annotations

import io
import hashlib
import json
import shutil
import sqlite3
import unittest
import wave
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import numpy as np

from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
from allday_asr.v3.adapters.sqlite import LATEST_V3_SCHEMA_VERSION, SqliteUnitOfWork
from allday_asr.v3.application.durable_processing import CorrectUtteranceCommand
from allday_asr.v3.application.knowledge import KnowledgeArchitectureService
from allday_asr.v3.bootstrap import V3CorePaths, compose_v3_core
from allday_asr.v3.config import CodexReminderSettings
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.knowledge import (
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.domain.identity import IdentityDecision, SelfIdentity
from allday_asr.v3.domain.people import (
    PersonIdentityMaturity,
    PersonIdentityPolicy,
    PersonKind,
    RepresentativeClip,
    SpeakerEmbedding,
    conservative_match,
    layered_person_match,
)
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


TEST_STATE = Path(__file__).parents[1] / "state"
NOW = datetime(2026, 9, 1, 1, 2, 3, tzinfo=timezone.utc)
NOW_TEXT = NOW.isoformat()
UTTERANCE_END_TEXT = (NOW + timedelta(seconds=5)).isoformat()


def _session_id(number: int) -> str:
    return f"{100 + number:026d}"


def _track_id(number: int, *, reprocessed: bool = False) -> str:
    return f"{300 + number + (100 if reprocessed else 0):026d}"


def _utterance_id(number: int, *, reprocessed: bool = False) -> str:
    return f"{500 + number + (100 if reprocessed else 0):026d}"


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


class FakeCalibratedSelfMatcher:
    def status(self):
        return {
            "available": True,
            "auto_identity_enabled": True,
            "reference_count": 82,
            "policy_version": "fixture-self-policy",
        }

    def match(self, embedding):
        matched = embedding.vector[1] > 0.9
        identity = SelfIdentity.SELF if matched else SelfIdentity.UNKNOWN
        return IdentityDecision(
            identity,
            {
                "source": "calibrated_self_voiceprint",
                "decision": identity.value,
                "reason": "fixture",
                "score": 0.97 if matched else 0.1,
            },
        )


class V34OpenSpeakerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TEST_STATE / f"v34-people-{uuid4().hex}"
        self.paths = V3CorePaths.from_state_dir(self.root)
        self.provider = FakeEmbeddingProvider(
            {
                _track_id(1): (1.0, 0.0, 0.0),
                _track_id(2): (0.999, 0.02, 0.0),
                _track_id(3): (0.998, 0.03, 0.0),
                _track_id(4): (0.0, 1.0, 0.0),
            }
        )
        self.core = compose_v3_core(
            self.paths,
            codex_settings=CodexReminderSettings(enabled=False),
            speaker_embedding_provider=self.provider,
        )
        self.assertEqual(self.core.initialize(), LATEST_V3_SCHEMA_VERSION)
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
        first = self.core.people.analyze(_session_id(1))
        self.assertEqual(first["new_cluster_count"], 1)
        cluster_id = self.core.people.list_clusters()[0]["cluster_id"]

        self._seed_track(2)
        second = self.core.people.analyze(_session_id(2))
        self.assertEqual(second["matched_track_count"], 1)
        cluster = self.core.people.cluster(cluster_id)
        self.assertEqual(cluster["session_count"], 2)
        self.assertEqual(cluster["track_count"], 2)

        person = self.core.people.create_person("张老师")
        labeled = self.core.people.label_cluster(cluster_id, person["person_id"])
        self.assertEqual(labeled["person_id"], person["person_id"])
        self.assertEqual(labeled["updated_utterance_count"], 2)
        with self.core.database.transaction() as connection:
            identities = {
                row[0]
                for row in connection.execute(
                    "SELECT identity FROM utterances WHERE utterance_id IN (?, ?)",
                    (_utterance_id(1), _utterance_id(2)),
                )
            }
        self.assertEqual(identities, {"not_self"})
        self.assertEqual(self.core.people.list_people()[0]["prototype_count"], 0)
        candidates = self.core.people.list_review_candidates(person["person_id"])
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.core.people.review_prototype(
                candidate["prototype_id"], person["person_id"], "confirmed"
            )
        self.assertEqual(self.core.people.list_people()[0]["prototype_count"], 2)

        self._seed_track(3)
        third = self.core.people.analyze(_session_id(3))
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
        self.core.people.analyze(_session_id(1))
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
                input_scope={"session_id": _session_id(1)},
                proposals=(
                    (
                        ProposalKind.EVENT_OPERATION,
                        {
                            "operation": "create",
                            "session_id": _session_id(1),
                            "event_kind": "task",
                            "expected_revision": 0,
                            "patch": {
                                "title": "回电话",
                                "actor_person_id": cluster_id,
                                "related_person_ids": [cluster_id],
                            },
                        },
                        (_utterance_id(1),),
                    ),
                ),
            )
        )
        knowledge.accept_proposal(generation["proposals"][0]["proposal_id"], "fixture")
        person = self.core.people.create_person("王同学")

        result = self.core.people.label_cluster(cluster_id, person["person_id"])

        self.assertEqual(len(result["rebound_event_ids"]), 1)
        event = knowledge.list_events(_session_id(1))[0]
        self.assertEqual(event["payload"]["actor_person_id"], person["person_id"])
        self.assertEqual(event["payload"]["related_person_ids"], [person["person_id"]])
        self.assertEqual(event["revision"], 2)
        self.assertEqual(len(knowledge.event_history(event["event_id"])), 2)

        undone = self.core.people.undo(cluster_id)
        self.assertEqual(undone["rebound_event_ids"], [event["event_id"]])
        self.assertEqual(undone["updated_utterance_count"], 1)
        with self.core.database.transaction() as connection:
            identity = connection.execute(
                "SELECT identity FROM utterances WHERE utterance_id = ?",
                (_utterance_id(1),),
            ).fetchone()[0]
        self.assertEqual(identity, "unknown")
        restored = knowledge.list_events(_session_id(1))[0]
        self.assertEqual(restored["payload"]["actor_person_id"], cluster_id)
        self.assertEqual(restored["revision"], 3)

    def test_self_label_updates_timeline_but_preserves_later_manual_identity(self) -> None:
        self._seed_track(1)
        self.core.people.analyze(_session_id(1))
        cluster_id = self.core.people.list_clusters()[0]["cluster_id"]
        self_person = self.core.people.create_person("我", PersonKind.SELF)

        labeled = self.core.people.label_cluster(cluster_id, self_person["person_id"])

        self.assertEqual(labeled["updated_utterance_count"], 1)
        with SqliteUnitOfWork(self.core.database) as uow:
            utterance = uow.evidence.get_utterance(_utterance_id(1))
        self.assertIs(utterance.identity, SelfIdentity.SELF)
        self.core.corrections.correct_utterance(
            CorrectUtteranceCommand(
                utterance_id=_utterance_id(1),
                expected_revision=utterance.revision,
                text=utterance.text,
                actor="desktop-user",
                identity=SelfIdentity.NOT_SELF,
                change_identity=True,
            )
        )

        undone = self.core.people.undo(cluster_id)

        self.assertEqual(undone["updated_utterance_count"], 0)
        self.assertEqual(undone["preserved_manual_utterance_count"], 1)
        with SqliteUnitOfWork(self.core.database) as uow:
            preserved = uow.evidence.get_utterance(_utterance_id(1))
        self.assertIs(preserved.identity, SelfIdentity.NOT_SELF)

    def test_analysis_uses_only_latest_canonical_processing_run(self) -> None:
        self._seed_track(1)
        self._seed_reprocessed_track(1)
        self.provider.vectors[_track_id(1, reprocessed=True)] = (1.0, 0.0, 0.0)

        analyzed = self.core.people.analyze(_session_id(1))

        self.assertEqual(analyzed["track_count"], 1)
        self.assertEqual(analyzed["embedded_track_count"], 1)
        detail = self.core.people.cluster(self.core.people.list_clusters()[0]["cluster_id"])
        self.assertEqual(
            [member["speaker_track_id"] for member in detail["members"]],
            [_track_id(1, reprocessed=True)],
        )

    def test_calibrated_self_voiceprint_auto_links_without_manual_click(self) -> None:
        self.core.close()
        self.core = compose_v3_core(
            self.paths,
            codex_settings=CodexReminderSettings(enabled=False),
            speaker_embedding_provider=self.provider,
            self_identity_matcher=FakeCalibratedSelfMatcher(),
        )
        self.core.initialize()
        self._seed_track(4)
        self_person = self.core.people.create_person("我", PersonKind.SELF)

        analyzed = self.core.people.analyze(_session_id(4))

        self.assertEqual(analyzed["auto_identified_self_cluster_count"], 1)
        self.assertEqual(analyzed["auto_identity_updated_utterance_count"], 1)
        cluster = self.core.people.list_clusters()[0]
        self.assertEqual(cluster["person_id"], self_person["person_id"])
        self.assertEqual(cluster["link_source"], "automatic")
        self.assertEqual(cluster["session_ids"], [_session_id(4)])
        with self.core.database.transaction() as connection:
            identity = connection.execute(
                "SELECT identity FROM utterances WHERE utterance_id = ?",
                (_utterance_id(4),),
            ).fetchone()[0]
            accepted = connection.execute(
                "SELECT COUNT(*) FROM voice_prototypes WHERE status = 'accepted'"
            ).fetchone()[0]
        self.assertEqual(identity, "self")
        self.assertEqual(accepted, 0)
        person = self.core.people.list_people()[0]
        self.assertEqual(person["enrollment_reference_count"], 82)
        self.assertTrue(person["auto_identity_enabled"])

        self.core.people.label_cluster(cluster["cluster_id"], self_person["person_id"])
        confirmed = self.core.people.list_clusters()[0]
        self.assertEqual(confirmed["link_source"], "human")
        self.assertEqual(self.core.people.list_people()[0]["prototype_count"], 0)

    def test_only_reviewed_prototype_enters_stable_library_and_retraction_removes_it(self) -> None:
        self._seed_track(1)
        self.core.people.analyze(_session_id(1))
        cluster_id = self.core.people.list_clusters()[0]["cluster_id"]
        person = self.core.people.create_person("陈老师")

        self.core.people.label_cluster(cluster_id, person["person_id"])

        self.assertEqual(self.core.people.list_people()[0]["prototype_count"], 0)
        candidate = self.core.people.list_review_candidates(person["person_id"])[0]
        self.core.people.review_prototype(
            candidate["prototype_id"], person["person_id"], "uncertain"
        )
        pending = self.core.people.list_review_candidates(person["person_id"])
        self.assertEqual(pending[0]["review_status"], "uncertain")
        confirmed = self.core.people.review_prototype(
            candidate["prototype_id"], person["person_id"], "confirmed"
        )
        self.assertIsNotNone(confirmed["accepted_prototype_id"])
        summary = self.core.people.list_people()[0]
        self.assertEqual(summary["prototype_count"], 1)
        self.assertEqual(summary["voice_maturity_status"], "learning")

        self.core.people.review_prototype(
            candidate["prototype_id"], person["person_id"], "retracted"
        )

        summary = self.core.people.list_people()[0]
        self.assertEqual(summary["prototype_count"], 0)
        self.assertEqual(summary["voice_maturity_status"], "seed")

    def test_confirmed_historical_windows_create_one_idempotent_v3_seed(self) -> None:
        self._seed_track(1)
        person = self.core.people.create_person("母亲")
        source_ref = "fixture:confirmed-speaker-enrollment:mother:1"
        enrollment_track_id = stable_ulid("confirmed-enrollment-track", source_ref)
        self.provider.vectors[enrollment_track_id] = (1.0, 0.0, 0.0)

        created = self.core.people.enroll_confirmed_windows(
            person["person_id"],
            _session_id(1),
            ((1_000, 3_000), (4_000, 6_000)),
            source_ref=source_ref,
            display_label="历史人工声纹 · 母亲",
            actor="system:test-migration",
        )
        repeated = self.core.people.enroll_confirmed_windows(
            person["person_id"],
            _session_id(1),
            ((1_000, 3_000), (4_000, 6_000)),
            source_ref=source_ref,
            display_label="历史人工声纹 · 母亲",
            actor="system:test-migration",
        )

        self.assertEqual(created["state"], "created")
        self.assertEqual(repeated["state"], "existing")
        summary = self.core.people.list_people()[0]
        self.assertEqual(summary["prototype_count"], 1)
        self.assertEqual(summary["voice_maturity_status"], "learning")
        self.core.people.review_prototype(
            created["prototype_id"], person["person_id"], "retracted"
        )
        preserved = self.core.people.enroll_confirmed_windows(
            person["person_id"],
            _session_id(1),
            ((1_000, 3_000), (4_000, 6_000)),
            source_ref=source_ref,
            display_label="历史人工声纹 · 母亲",
            actor="system:test-migration",
        )
        self.assertEqual(preserved["state"], "preserved_retracted")
        self.assertEqual(self.core.people.list_people()[0]["prototype_count"], 0)
        with self.core.database.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM speaker_tracks WHERE speaker_track_id = ?",
                    (enrollment_track_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM utterances WHERE speaker_track_id = ?",
                    (enrollment_track_id,),
                ).fetchone()[0],
                0,
            )

    def test_daily_confirmations_and_hard_negatives_unlock_opt_in_auto_matching(self) -> None:
        for number, vector in {
            1: (1.0, 0.0, 0.0),
            2: (0.999, 0.02, 0.0),
            3: (0.998, 0.03, 0.0),
            5: (0.0, 1.0, 0.0),
            6: (0.0, 0.0, 1.0),
            7: (0.999, -0.02, 0.0),
        }.items():
            self.provider.vectors[_track_id(number)] = vector
        person = self.core.people.create_person("林老师")

        for number in (1, 2, 3):
            self._seed_track(number)
            self.core.people.analyze(_session_id(number))
            if number == 1:
                cluster = next(
                    value
                    for value in self.core.people.list_clusters()
                    if _session_id(number) in value["session_ids"]
                )
                self.core.people.label_cluster(
                    cluster["cluster_id"], person["person_id"]
                )
            candidate = self.core.people.list_review_candidates(
                person["person_id"]
            )[0]
            self.core.people.review_prototype(
                candidate["prototype_id"], person["person_id"], "confirmed"
            )

        for number in (5, 6):
            self._seed_track(number)
            self.core.people.analyze(_session_id(number))
            candidate = self.core.people.list_review_candidates(
                person["person_id"]
            )[0]
            self.core.people.review_prototype(
                candidate["prototype_id"], person["person_id"], "rejected"
            )

        summary = self.core.people.list_people()[0]
        self.assertEqual(summary["voice_maturity_status"], "calibrated")
        self.assertEqual(summary["rejected_prototype_count"], 2)
        self.assertFalse(summary["auto_identity_enabled"])
        self.core.people.update_identity_policy(
            person["person_id"], auto_match_enabled=True
        )

        self._seed_track(7)
        analyzed = self.core.people.analyze(_session_id(7))

        self.assertEqual(analyzed["auto_identified_known_cluster_count"], 1)
        cluster = next(
            value
            for value in self.core.people.list_clusters()
            if _session_id(7) in value["session_ids"]
        )
        self.assertEqual(cluster["person_id"], person["person_id"])
        self.assertEqual(cluster["link_source"], "automatic")

    def test_calibrated_self_match_stays_out_of_ambiguous_anonymous_cluster(self) -> None:
        self.core.close()
        self.provider.vectors[_track_id(1)] = (0.5, 0.866, 0.0)
        self.core = compose_v3_core(
            self.paths,
            codex_settings=CodexReminderSettings(enabled=False),
            speaker_embedding_provider=self.provider,
            self_identity_matcher=FakeCalibratedSelfMatcher(),
        )
        self.core.initialize()
        self_person = self.core.people.create_person("我", PersonKind.SELF)
        self._seed_track(1)
        first = self.core.people.analyze(_session_id(1))
        self.assertEqual(first["new_cluster_count"], 1)
        self.assertEqual(first["auto_identified_self_cluster_count"], 0)

        self._seed_track(4)
        second = self.core.people.analyze(_session_id(4))

        self.assertEqual(second["new_cluster_count"], 1)
        self.assertEqual(second["matched_track_count"], 0)
        self.assertEqual(second["auto_identified_self_cluster_count"], 1)
        clusters = self.core.people.list_clusters()
        self.assertEqual(len(clusters), 2)
        self_cluster = next(
            cluster for cluster in clusters if cluster["person_id"] == self_person["person_id"]
        )
        self.assertEqual(self_cluster["track_count"], 1)
        self.assertEqual(self_cluster["session_ids"], [_session_id(4)])

    def test_calibrated_self_matcher_loads_policy_bound_voiceprint(self) -> None:
        identity_root = self.paths.state_dir / "identity"
        identity_root.mkdir(parents=True, exist_ok=True)
        voiceprint = identity_root / "fixture-self.npz"
        references = np.asarray(
            [[1.0, 0.0, 0.0], [0.98, 0.02, 0.0], [0.99, -0.01, 0.0]],
            dtype=np.float32,
        )
        references /= np.linalg.norm(references, axis=1, keepdims=True)
        centroid = references.mean(axis=0)
        centroid /= np.linalg.norm(centroid)
        np.savez_compressed(
            voiceprint,
            embeddings=references,
            centroid=centroid,
            metadata_json=np.asarray("{}"),
        )
        policy = {
            "accepted": True,
            "blockers": [],
            "policy_version": "fixture-policy",
            "self_threshold": 0.8,
            "not_self_threshold": 0.2,
            "false_accept_rate": 0.0,
            "false_reject_rate": 0.0,
            "voiceprint": str(voiceprint.resolve()),
            "voiceprint_sha256": hashlib.sha256(voiceprint.read_bytes()).hexdigest(),
        }
        (identity_root / "active-self-identity-policy.json").write_text(
            json.dumps(policy), encoding="utf-8"
        )
        matcher = CalibratedSelfIdentityMatcher(self.paths.state_dir)

        decision = matcher.match(
            SpeakerEmbedding(
                speaker_track_id="fixture",
                model="FunASR/CAM++",
                model_version="v1-local",
                vector=(1.0, 0.0, 0.0),
                representatives=(),
                quality_score=0.95,
            )
        )

        self.assertIs(decision.identity, SelfIdentity.SELF)
        self.assertGreater(decision.evidence["score"], 0.99)
        self.assertEqual(matcher.status()["reference_count"], 3)

    def test_split_ignore_and_undo_are_reversible_without_mutating_prototypes(self) -> None:
        self._seed_track(1)
        self._seed_track(2)
        self.core.people.analyze(_session_id(1))
        self.core.people.analyze(_session_id(2))
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

    def test_known_person_match_uses_four_decision_tiers(self) -> None:
        person_id = "person-a"
        learning = PersonIdentityPolicy(
            person_id=person_id,
            maturity_status=PersonIdentityMaturity.LEARNING,
        )
        calibrated = PersonIdentityPolicy(
            person_id=person_id,
            maturity_status=PersonIdentityMaturity.CALIBRATED,
            auto_match_enabled=True,
        )
        candidates = ((person_id, (1.0, 0.0)),)

        insufficient = layered_person_match(
            (1.0, 0.0), candidates, {person_id: learning}, quality_score=0.2
        )
        automatic = layered_person_match(
            (1.0, 0.0), candidates, {person_id: calibrated}, quality_score=0.9
        )
        suggested = layered_person_match(
            (1.0, 0.0), candidates, {person_id: learning}, quality_score=0.9
        )
        other = layered_person_match(
            (0.0, 1.0), candidates, {person_id: learning}, quality_score=0.9
        )

        self.assertEqual(insufficient.tier.value, "insufficient_evidence")
        self.assertEqual(automatic.tier.value, "auto_matched")
        self.assertEqual(suggested.tier.value, "suggested")
        self.assertEqual(other.tier.value, "no_known_match")
        self.assertEqual(other.candidate_person_id, person_id)

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
        session_id = _session_id(number)
        run_id = f"run-{number}"
        artifact_id = f"artifact-{number}"
        asset_id = f"asset-{number}"
        replica_id = f"replica-{number}"
        track_id = _track_id(number)
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
                    _utterance_id(number),
                    session_id,
                    run_id,
                    artifact_id,
                    track_id,
                    track_id,
                    NOW_TEXT,
                    UTTERANCE_END_TEXT,
                    '{"source":"none","decision":"unknown"}',
                    NOW_TEXT,
                    NOW_TEXT,
                ),
            )

    def _seed_reprocessed_track(self, number: int) -> None:
        created = NOW + timedelta(minutes=1)
        created_text = created.isoformat()
        end_text = (created + timedelta(seconds=5)).isoformat()
        with self.core.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO processing_runs (
                  run_id, session_id, pipeline_version, input_revision, status,
                  config_digest, current_stage, progress, completed_at, created_at, updated_at
                ) VALUES (?, ?, 'fixture', 1, 'succeeded', ?, 'complete', 1, ?, ?, ?)
                """,
                (
                    f"run-{number}-new",
                    _session_id(number),
                    "b" * 64,
                    created_text,
                    created_text,
                    created_text,
                ),
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
                    f"artifact-{number}-new",
                    f"run-{number}-new",
                    "b" * 64,
                    f"artifact/{number}-new",
                    f"{number + 200:064x}",
                    created_text,
                ),
            )
            connection.execute(
                """
                INSERT INTO speaker_tracks (
                  speaker_track_id, session_id, run_id, label, source_artifact_id, created_at
                ) VALUES (?, ?, ?, 'speaker_01', ?, ?)
                """,
                (
                    _track_id(number, reprocessed=True),
                    _session_id(number),
                    f"run-{number}-new",
                    f"artifact-{number}-new",
                    created_text,
                ),
            )
            connection.execute(
                """
                INSERT INTO utterances (
                  utterance_id, session_id, run_id, source_artifact_id,
                  speaker_track_id, original_speaker_track_id, ordinal,
                  start_ms, end_ms, start_at, end_at, text, original_text,
                  identity, original_identity, identity_evidence_json, evidence_json,
                  revision, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, 1000, 6000, ?, ?, '新处理声音',
                  '新处理声音', 'unknown', 'unknown', ?, '{}', 1, 'active', ?, ?)
                """,
                (
                    _utterance_id(number, reprocessed=True),
                    _session_id(number),
                    f"run-{number}-new",
                    f"artifact-{number}-new",
                    _track_id(number, reprocessed=True),
                    _track_id(number, reprocessed=True),
                    created_text,
                    end_text,
                    '{"source":"none","decision":"unknown"}',
                    created_text,
                    created_text,
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
