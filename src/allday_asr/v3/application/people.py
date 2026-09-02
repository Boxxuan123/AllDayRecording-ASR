from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.knowledge import (
    GenerationSubmission,
    KnowledgeLayer,
    ProposalKind,
)
from allday_asr.v3.domain.people import (
    ClusterMatchPolicy,
    LayeredMatchDecision,
    PersonIdentityMaturity,
    PersonIdentityPolicy,
    PersonKind,
    SpeakerEmbedding,
    SpeakerMatchTier,
    conservative_match,
    cosine_similarity,
    layered_person_match,
)
from allday_asr.v3.domain.processing import SpeakerTrack
from allday_asr.v3.ports.repositories import UnitOfWork
from allday_asr.v3.ports.self_identity_matching import SelfIdentityMatcher
from allday_asr.v3.ports.speaker_embeddings import SpeakerEmbeddingProvider

from allday_asr.v3.application.durable_processing import (
    CorrectUtteranceCommand,
    apply_utterance_correction,
)
from allday_asr.v3.application.knowledge import KnowledgeArchitectureService


UnitOfWorkFactory = Callable[[], UnitOfWork]
DateTimeClock = Callable[[], datetime]
_CLUSTER_IDENTITY_ACTOR_PREFIX = "system:speaker-cluster-identity:"
_KNOWN_PERSON_POLICY_VERSION = "known-person-layered-v1"


class SpeakerIdentityService:
    """Open-set identity workflow with calibrated automatic self recognition."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embedding_provider: SpeakerEmbeddingProvider,
        knowledge: KnowledgeArchitectureService,
        *,
        policy: ClusterMatchPolicy | None = None,
        self_identity_matcher: SelfIdentityMatcher | None = None,
        now: DateTimeClock | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._provider = embedding_provider
        self._knowledge = knowledge
        self._policy = policy or ClusterMatchPolicy()
        self._self_identity_matcher = self_identity_matcher
        self._now = now or (lambda: datetime.now(timezone.utc))

    def analyze(self, session_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            tracks = uow.people.analysis_inputs(session_id)
            run_id = new_ulid()
            started_at = _datetime(self._now())
            uow.people.start_run(
                run_id,
                session_id,
                self._provider.model,
                self._provider.model_version,
                self._policy_dict(),
                len(tracks),
                started_at,
            )
        try:
            embeddings = self._provider.embed(tracks)
            embedded_ids = {value.speaker_track_id for value in embeddings}
            missing = sorted(
                track.speaker_track_id for track in tracks
                if track.speaker_track_id not in embedded_ids
            )
            created = 0
            matched = 0
            with self._uow_factory() as uow:
                self_person_id = uow.people.self_person_id()
                for index, embedding in enumerate(embeddings, start=1):
                    self_match = (
                        self._self_identity_matcher.match(embedding)
                        if self_person_id is not None
                        and self._self_identity_matcher is not None
                        else None
                    )
                    # A calibrated self match must not be absorbed into an anonymous
                    # cluster whose older prototypes may belong to somebody else.
                    # Keeping it isolated lets reconcile_self validate and link it
                    # without broadening the identity to a mixed cluster.
                    anonymous = (
                        ()
                        if self_match is not None
                        and self_match.identity is SelfIdentity.SELF
                        else _compatible(
                            embedding.vector,
                            uow.people.cluster_vectors(
                                embedding.model, embedding.model_version
                            ),
                        )
                    )
                    anonymous_match = conservative_match(
                        embedding.vector,
                        anonymous,
                        threshold=self._policy.anonymous_threshold,
                        minimum_margin=self._policy.minimum_margin,
                    )
                    create_cluster = anonymous_match.target_id is None
                    cluster_id = anonymous_match.target_id or new_ulid()
                    if create_cluster:
                        created += 1
                    else:
                        matched += 1
                    suggested_person_id = None
                    suggestion_confidence = None
                    if (
                        self_person_id is not None
                        and self_match is not None
                        and self_match.identity is SelfIdentity.SELF
                    ):
                        suggested_person_id = self_person_id
                        suggestion_confidence = _identity_confidence(self_match.evidence)
                    uow.people.record_embedding(
                        run_id=run_id,
                        cluster_id=cluster_id,
                        cluster_label=f"未知说话人 {index:02d}",
                        create_cluster=create_cluster,
                        membership_id=new_ulid(),
                        prototype_id=new_ulid(),
                        operation_id=new_ulid(),
                        embedding=embedding,
                        membership_confidence=(anonymous_match.score or 1.0),
                        suggested_person_id=suggested_person_id,
                        suggestion_confidence=(
                            suggestion_confidence
                            if suggested_person_id is not None
                            else None
                        ),
                        created_at=_datetime(self._now()),
                    )
                uow.people.finish_run(
                    run_id, "succeeded", _datetime(self._now()), None
                )
            automatic = self.reconcile_self(session_id)
            known_people = self.rematch_existing(
                session_id, trigger="initial_analysis"
            )
            return {
                "cluster_run_id": run_id,
                "session_id": session_id,
                "status": "succeeded",
                "track_count": len(tracks),
                "embedded_track_count": len(embeddings),
                "new_cluster_count": created,
                "matched_track_count": matched,
                "person_suggestion_count": known_people["suggested_cluster_count"],
                "unusable_track_ids": missing,
                **automatic,
                **known_people,
            }
        except Exception as exc:
            with self._uow_factory() as uow:
                uow.people.finish_run(
                    run_id, "failed", _datetime(self._now()), str(exc)[:2_000]
                )
            raise

    def reconcile_self(self, session_id: str | None = None) -> dict[str, int]:
        """Auto-link only clusters whose eligible prototypes all match calibrated self."""

        if self._self_identity_matcher is None:
            return {
                "auto_identified_self_cluster_count": 0,
                "auto_identity_updated_utterance_count": 0,
            }
        matcher_status = self._self_identity_matcher.status()
        if not matcher_status.get("auto_identity_enabled"):
            return {
                "auto_identified_self_cluster_count": 0,
                "auto_identity_updated_utterance_count": 0,
            }
        with self._uow_factory() as uow:
            person_id = uow.people.self_person_id()
            candidates = uow.people.unlinked_cluster_embeddings(session_id)
        if person_id is None:
            return {
                "auto_identified_self_cluster_count": 0,
                "auto_identity_updated_utterance_count": 0,
            }
        grouped: dict[str, list[SpeakerEmbedding]] = {}
        for candidate in candidates:
            grouped.setdefault(str(candidate["cluster_id"]), []).append(
                SpeakerEmbedding(
                    speaker_track_id=str(candidate["speaker_track_id"]),
                    model=str(candidate["model"]),
                    model_version=str(candidate["model_version"]),
                    vector=tuple(candidate["vector"]),
                    representatives=(),
                    quality_score=float(candidate["quality_score"]),
                )
            )
        linked = 0
        updated = 0
        for cluster_id, embeddings in grouped.items():
            decisions = tuple(
                self._self_identity_matcher.match(embedding)
                for embedding in embeddings
            )
            if not decisions or any(
                decision.identity is not SelfIdentity.SELF for decision in decisions
            ):
                continue
            confidence = min(
                _identity_confidence(decision.evidence) for decision in decisions
            )
            result = self._link_cluster(
                cluster_id,
                person_id,
                actor="system:calibrated-self-identity",
                source="automatic",
                confidence=confidence,
                promote_candidates=False,
            )
            linked += 1
            updated += int(result["updated_utterance_count"])
        return {
            "auto_identified_self_cluster_count": linked,
            "auto_identity_updated_utterance_count": updated,
        }

    def rematch_existing(
        self,
        session_id: str | None = None,
        *,
        trigger: str = "historical_rematch",
    ) -> dict[str, int]:
        """Re-evaluate anonymous prototypes without promoting any of them."""

        if trigger not in {
            "initial_analysis",
            "historical_rematch",
            "prototype_review",
            "policy_change",
        }:
            raise ValueError("speaker rematch trigger is invalid")
        with self._uow_factory() as uow:
            candidates = uow.people.unlinked_cluster_embeddings(session_id)
            policies = _identity_policies(uow.people.identity_policies())
            self_person_id = uow.people.self_person_id()
            vector_cache: dict[
                tuple[str, str], tuple[tuple[str, tuple[float, ...]], ...]
            ] = {}
            grouped: dict[str, list[LayeredMatchDecision]] = {}
            created_at = _datetime(self._now())
            for candidate in candidates:
                model_key = (str(candidate["model"]), str(candidate["model_version"]))
                people = vector_cache.get(model_key)
                if people is None:
                    people = uow.people.person_vectors(*model_key)
                    vector_cache[model_key] = people
                vector = tuple(candidate["vector"])
                decision = layered_person_match(
                    vector,
                    _compatible(vector, people),
                    policies,
                    quality_score=float(candidate["quality_score"]),
                )
                cluster_id = str(candidate["cluster_id"])
                grouped.setdefault(cluster_id, []).append(decision)
                uow.people.record_match_decision(
                    decision_id=new_ulid(),
                    cluster_id=cluster_id,
                    prototype_id=str(candidate["prototype_id"]),
                    speaker_track_id=str(candidate["speaker_track_id"]),
                    decision_tier=decision.tier.value,
                    candidate_person_id=decision.candidate_person_id,
                    best_score=decision.best_score,
                    second_best_score=decision.second_best_score,
                    score_margin=decision.score_margin,
                    quality_score=float(candidate["quality_score"]),
                    policy_revision=decision.policy_revision,
                    policy_version=_KNOWN_PERSON_POLICY_VERSION,
                    trigger=trigger,
                    reason=decision.reason,
                    created_at=created_at,
                )

            auto_links: list[tuple[str, str, float]] = []
            suggestion_count = 0
            for cluster_id, decisions in grouped.items():
                resolved = {
                    decision.candidate_person_id
                    for decision in decisions
                    if decision.tier
                    in {SpeakerMatchTier.AUTO_MATCHED, SpeakerMatchTier.SUGGESTED}
                }
                all_resolved = all(
                    decision.tier
                    in {SpeakerMatchTier.AUTO_MATCHED, SpeakerMatchTier.SUGGESTED}
                    for decision in decisions
                )
                if all_resolved and len(resolved) == 1:
                    person_id = next(iter(resolved))
                    confidence = min(
                        float(decision.best_score or 0.0) for decision in decisions
                    )
                    uow.people.set_cluster_suggestion(
                        cluster_id, person_id, confidence, created_at
                    )
                    suggestion_count += 1
                    if all(
                        decision.tier is SpeakerMatchTier.AUTO_MATCHED
                        for decision in decisions
                    ):
                        auto_links.append((cluster_id, str(person_id), confidence))
                elif not any(
                    str(candidate.get("cluster_id")) == cluster_id
                    and candidate.get("suggested_person_id") == self_person_id
                    for candidate in candidates
                ):
                    uow.people.set_cluster_suggestion(
                        cluster_id, None, None, created_at
                    )

        linked = 0
        updated = 0
        for cluster_id, person_id, confidence in auto_links:
            result = self._link_cluster(
                cluster_id,
                person_id,
                actor="system:calibrated-known-person-identity",
                source="automatic",
                confidence=confidence,
                promote_candidates=False,
            )
            linked += 1
            updated += int(result["updated_utterance_count"])
        return {
            "rematched_prototype_count": len(candidates),
            "suggested_cluster_count": suggestion_count,
            "auto_identified_known_cluster_count": linked,
            "auto_known_identity_updated_utterance_count": updated,
        }

    def list_people(self) -> tuple[dict[str, Any], ...]:
        with self._uow_factory() as uow:
            people = uow.people.list_people()
            counts = uow.person_memories.summary_counts()
        self_status = (
            self._self_identity_matcher.status()
            if self._self_identity_matcher is not None
            else {
                "auto_identity_enabled": False,
                "reference_count": 0,
                "policy_version": None,
            }
        )
        return tuple(
            {
                **person,
                **counts.get(
                    str(person["person_id"]),
                    {
                        "memory_count": 0,
                        "interaction_count": 0,
                        "last_interaction_at": None,
                    },
                ),
                "enrollment_reference_count": (
                    int(self_status.get("reference_count", 0))
                    if person["kind"] == PersonKind.SELF.value
                    else 0
                ),
                "auto_identity_enabled": (
                    bool(self_status.get("auto_identity_enabled"))
                    if person["kind"] == PersonKind.SELF.value
                    else bool(person.get("known_auto_match_enabled"))
                ),
                "identity_policy_version": (
                    self_status.get("policy_version")
                    if person["kind"] == PersonKind.SELF.value
                    else (
                        f"{_KNOWN_PERSON_POLICY_VERSION}:r{person['voice_policy_revision']}"
                        if person.get("voice_policy_revision") is not None
                        else None
                    )
                ),
            }
            for person in people
        )

    def list_clusters(
        self, status: str | None = None, limit: int = 100
    ) -> tuple[dict[str, Any], ...]:
        if status is not None and status not in {"active", "merged", "split", "ignored"}:
            raise ValueError("speaker cluster status is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("speaker cluster limit must be between 1 and 500")
        with self._uow_factory() as uow:
            return uow.people.list_clusters(status, limit)

    def list_review_candidates(
        self,
        person_id: str | None = None,
        status: str | None = "pending",
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        if status not in {
            None,
            "pending",
            "confirmed",
            "rejected",
            "uncertain",
            "retracted",
        }:
            raise ValueError("voice prototype review status is invalid")
        if not 1 <= limit <= 500:
            raise ValueError("voice prototype review limit must be between 1 and 500")
        with self._uow_factory() as uow:
            if person_id is not None and uow.people.person_kind(person_id) != "known":
                raise ValueError("voice prototype review queue only supports known people")
            return uow.people.list_review_candidates(person_id, status, limit)

    def enroll_confirmed_windows(
        self,
        person_id: str,
        session_id: str,
        windows: tuple[tuple[int, int], ...],
        *,
        source_ref: str,
        display_label: str,
        actor: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Create one V3-native voice seed from already human-confirmed audio."""

        if not source_ref.strip():
            raise ValueError("confirmed enrollment source reference is required")
        if not actor.strip():
            raise ValueError("confirmed enrollment actor is required")
        selected = tuple(
            sorted(
                sorted(set(windows), key=lambda value: value[1] - value[0], reverse=True)[
                    :12
                ]
            )
        )
        if not selected:
            raise ValueError("confirmed enrollment requires at least one window")
        if any(start < 0 or end - start < 800 for start, end in selected):
            raise ValueError("confirmed enrollment windows must contain 800 ms of audio")

        track_id = stable_ulid("confirmed-enrollment-track", source_ref)
        cluster_id = stable_ulid("confirmed-enrollment-cluster", source_ref)
        prototype_id = stable_ulid("confirmed-enrollment-prototype", source_ref)
        membership_id = stable_ulid("confirmed-enrollment-membership", source_ref)
        operation_id = stable_ulid("confirmed-enrollment-operation", source_ref)
        with self._uow_factory() as uow:
            if uow.people.person_kind(person_id) != PersonKind.KNOWN.value:
                raise ValueError("confirmed enrollment only supports known people")
            try:
                candidate = uow.people.prototype_candidate(prototype_id)
            except KeyError:
                candidate = None
            if candidate is not None:
                current = uow.people.latest_prototype_review(prototype_id, person_id)
                if current is not None:
                    state = (
                        "existing"
                        if current["decision"] == "confirmed"
                        else f"preserved_{current['decision']}"
                    )
                    return {
                        "state": state,
                        "source_ref": source_ref,
                        "person_id": person_id,
                        "session_id": session_id,
                        "speaker_track_id": track_id,
                        "cluster_id": str(candidate["active_cluster_id"]),
                        "prototype_id": prototype_id,
                        "review_id": str(current["review_id"]),
                        "window_count": len(selected),
                    }
                enrollment = None
            else:
                enrollment = uow.people.confirmed_enrollment_input(
                    session_id, track_id, selected
                )

        if candidate is None:
            embedded = self._provider.embed((enrollment["track"],))
            if len(embedded) != 1:
                raise RuntimeError("confirmed enrollment did not produce one embedding")
            created_at = self._now()
            with self._uow_factory() as uow:
                added = uow.evidence.add_speaker_track(
                    SpeakerTrack(
                        speaker_track_id=track_id,
                        session_id=session_id,
                        run_id=str(enrollment["run_id"]),
                        label=f"human-enrollment-{track_id}",
                        source_artifact_id=str(enrollment["source_artifact_id"]),
                        created_at=created_at,
                    )
                )
                if not added:
                    raise RuntimeError("confirmed enrollment track already exists without a seed")
                uow.people.record_embedding(
                    run_id=str(enrollment["run_id"]),
                    cluster_id=cluster_id,
                    cluster_label=display_label.strip() or "历史人工声纹",
                    create_cluster=True,
                    membership_id=membership_id,
                    prototype_id=prototype_id,
                    operation_id=operation_id,
                    embedding=embedded[0],
                    membership_confidence=1.0,
                    suggested_person_id=person_id,
                    suggestion_confidence=1.0,
                    created_at=_datetime(created_at),
                    membership_source="human",
                    actor=actor,
                )

        reviewed = self.review_prototype(
            prototype_id,
            person_id,
            "confirmed",
            note=(note.strip() or f"已从人工确认区间迁移：{source_ref}"),
            actor=actor,
        )
        return {
            "state": "created",
            "source_ref": source_ref,
            "session_id": session_id,
            "speaker_track_id": track_id,
            "window_count": len(selected),
            **reviewed,
        }

    def review_prototype(
        self,
        prototype_id: str,
        person_id: str,
        decision: str,
        *,
        note: str = "",
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        if decision not in {"confirmed", "rejected", "uncertain", "retracted"}:
            raise ValueError("voice prototype review decision is invalid")
        if not actor.strip():
            raise ValueError("voice prototype review actor is required")
        with self._uow_factory() as uow:
            if uow.people.person_kind(person_id) != PersonKind.KNOWN.value:
                raise ValueError("known-person prototype review cannot target self")
            candidate = uow.people.prototype_candidate(prototype_id)
            current_review = uow.people.latest_prototype_review(
                prototype_id, person_id
            )
            cluster = uow.people.cluster_detail(str(candidate["active_cluster_id"]))
        current_decision = (
            str(current_review["decision"]) if current_review is not None else None
        )
        if current_decision == decision:
            raise ValueError("voice prototype already has this review decision")
        if decision == "retracted" and current_decision != "confirmed":
            raise ValueError("only a confirmed prototype can be retracted")
        target_cluster_id = str(candidate["active_cluster_id"])
        linked_person_id = candidate.get("linked_person_id")
        if decision == "confirmed":
            if linked_person_id is not None and str(linked_person_id) != person_id:
                raise ValueError("prototype cluster is linked to another person")
            if linked_person_id is None:
                if int(cluster["track_count"]) > 1:
                    split = self.split(
                        target_cluster_id,
                        (str(candidate["speaker_track_id"]),),
                        actor,
                    )
                    target_cluster_id = str(split["new_cluster_id"])
                self._link_cluster(
                    target_cluster_id,
                    person_id,
                    actor=actor,
                    source="human",
                    confidence=1.0,
                    promote_candidates=False,
                )
        review_id = new_ulid()
        with self._uow_factory() as uow:
            review = uow.people.add_prototype_review(
                review_id=review_id,
                accepted_prototype_id=f"accepted-{review_id}",
                prototype_id=prototype_id,
                person_id=person_id,
                decision=decision,
                actor=actor,
                note=note,
                created_at=_datetime(self._now()),
            )
        policy = self._refresh_maturity(person_id, actor)
        rematch = self.rematch_existing(trigger="prototype_review")
        return {
            **review,
            "cluster_id": target_cluster_id,
            "identity_policy": policy,
            "historical_rematch": rematch,
        }

    def update_identity_policy(
        self,
        person_id: str,
        *,
        auto_match_enabled: bool,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            if uow.people.person_kind(person_id) != PersonKind.KNOWN.value:
                raise ValueError("known-person identity policy cannot target self")
            current = uow.people.identity_policy(person_id)
            if auto_match_enabled and current["maturity_status"] != "calibrated":
                raise ValueError("automatic matching requires calibrated voice samples")
            if bool(current["auto_match_enabled"]) == auto_match_enabled:
                policy = current
            else:
                policy = uow.people.add_identity_policy_revision(
                    person_id,
                    maturity_status=str(current["maturity_status"]),
                    auto_match_enabled=auto_match_enabled,
                    suggest_threshold=float(current["suggest_threshold"]),
                    auto_accept_threshold=float(current["auto_accept_threshold"]),
                    minimum_margin=float(current["minimum_margin"]),
                    minimum_quality=float(current["minimum_quality"]),
                    calibration=dict(current["calibration"]),
                    actor=actor,
                    created_at=_datetime(self._now()),
                )
        return {
            "person_id": person_id,
            "identity_policy": policy,
            "historical_rematch": self.rematch_existing(trigger="policy_change"),
        }

    def _refresh_maturity(
        self, person_id: str, actor: str
    ) -> dict[str, Any]:
        with self._uow_factory() as uow:
            current = uow.people.identity_policy(person_id)
            examples = uow.people.prototype_review_examples(person_id)
        positives = tuple(
            dict(value)
            for value in {
                str(item["source_prototype_id"]): item
                for item in examples["positive"]
            }.values()
        )
        negatives = tuple(examples["negative"])
        threshold = float(current["auto_accept_threshold"])
        positive_hits = 0
        cross_session_eligible = 0
        for positive in positives:
            comparisons = tuple(
                other
                for other in positives
                if other["source_prototype_id"] != positive["source_prototype_id"]
                and other["session_id"] != positive["session_id"]
                and other["model"] == positive["model"]
                and other["model_version"] == positive["model_version"]
                and len(other["vector"]) == len(positive["vector"])
            )
            if not comparisons:
                continue
            cross_session_eligible += 1
            if max(
                cosine_similarity(positive["vector"], other["vector"])
                for other in comparisons
            ) >= threshold:
                positive_hits += 1
        false_accepts = 0
        evaluated_negatives = 0
        for negative in negatives:
            comparisons = tuple(
                positive
                for positive in positives
                if positive["model"] == negative["model"]
                and positive["model_version"] == negative["model_version"]
                and len(positive["vector"]) == len(negative["vector"])
            )
            if not comparisons:
                continue
            evaluated_negatives += 1
            if max(
                cosine_similarity(negative["vector"], positive["vector"])
                for positive in comparisons
            ) >= threshold:
                false_accepts += 1
        recall = (
            positive_hits / cross_session_eligible
            if cross_session_eligible
            else 0.0
        )
        false_accept_rate = (
            false_accepts / evaluated_negatives if evaluated_negatives else 0.0
        )
        session_count = len({str(value["session_id"]) for value in positives})
        calibrated = (
            len(positives) >= 3
            and session_count >= 2
            and cross_session_eligible == len(positives)
            and recall >= 0.80
            and len(negatives) >= 2
            and evaluated_negatives == len(negatives)
            and false_accept_rate == 0.0
        )
        if current["maturity_status"] == PersonIdentityMaturity.SUSPENDED.value:
            maturity = PersonIdentityMaturity.SUSPENDED
        elif not positives:
            maturity = PersonIdentityMaturity.SEED
        elif calibrated:
            maturity = PersonIdentityMaturity.CALIBRATED
        else:
            maturity = PersonIdentityMaturity.LEARNING
        calibration = {
            "positive_count": len(positives),
            "positive_session_count": session_count,
            "negative_count": len(negatives),
            "cross_session_evaluated_count": cross_session_eligible,
            "positive_recall": round(recall, 6),
            "negative_evaluated_count": evaluated_negatives,
            "false_accept_count": false_accepts,
            "false_accept_rate": round(false_accept_rate, 6),
            "calibration_requirements": {
                "minimum_positive_count": 3,
                "minimum_positive_session_count": 2,
                "minimum_negative_count": 2,
                "minimum_positive_recall": 0.80,
                "maximum_false_accept_rate": 0.0,
            },
        }
        auto_match_enabled = (
            bool(current["auto_match_enabled"])
            and calibrated
            and maturity is PersonIdentityMaturity.CALIBRATED
        )
        if (
            str(current["maturity_status"]) == maturity.value
            and bool(current["auto_match_enabled"]) == auto_match_enabled
            and current["calibration"] == calibration
        ):
            return current
        with self._uow_factory() as uow:
            return uow.people.add_identity_policy_revision(
                person_id,
                maturity_status=maturity.value,
                auto_match_enabled=auto_match_enabled,
                suggest_threshold=float(current["suggest_threshold"]),
                auto_accept_threshold=float(current["auto_accept_threshold"]),
                minimum_margin=float(current["minimum_margin"]),
                minimum_quality=float(current["minimum_quality"]),
                calibration=calibration,
                actor=actor,
                created_at=_datetime(self._now()),
            )

    def cluster(self, cluster_id: str) -> dict[str, Any]:
        with self._uow_factory() as uow:
            return uow.people.cluster_detail(cluster_id)

    def create_person(self, display_name: str, kind: PersonKind = PersonKind.KNOWN) -> dict[str, Any]:
        name = display_name.strip()
        if not name:
            raise ValueError("person display name is required")
        if kind is PersonKind.UNKNOWN:
            raise ValueError("use an anonymous cluster instead of an unknown person")
        person_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.create_person(person_id, name, kind.value, _datetime(self._now()))
        return {"person_id": person_id, "display_name": name, "kind": kind.value}

    def label_cluster(
        self, cluster_id: str, person_id: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        return self._link_cluster(
            cluster_id,
            person_id,
            actor=actor,
            source="human",
            confidence=1.0,
            promote_candidates=False,
        )

    def _link_cluster(
        self,
        cluster_id: str,
        person_id: str,
        *,
        actor: str,
        source: str,
        confidence: float,
        promote_candidates: bool,
    ) -> dict[str, Any]:
        if not actor.strip():
            raise ValueError("speaker label actor is required")
        operation_id = new_ulid()
        now = self._now()
        with self._uow_factory() as uow:
            person_kind = uow.people.person_kind(person_id)
            previous_person_id, _, event_ids = uow.people.label_cluster(
                cluster_id,
                person_id,
                actor,
                operation_id,
                _datetime(now),
                source=source,
                confidence=confidence,
                promote_candidates=promote_candidates,
            )
            identity_changes = self._sync_cluster_identity(
                uow,
                cluster_id,
                _identity_for_person_kind(person_kind),
                actor,
                now,
            )
            events = uow.people.events_by_ids(event_ids)
        rebound: list[str] = []
        warnings: list[str] = []
        for event in events:
            with self._uow_factory() as uow:
                evidence_ids = uow.people.cluster_evidence_ids(
                    cluster_id, str(event["session_id"])
                )
            if not evidence_ids:
                warnings.append(f"event {event['event_id']} has no same-session evidence")
                continue
            try:
                self._rebind_event(
                    event,
                    previous_person_id or cluster_id,
                    person_id,
                    evidence_ids,
                    actor,
                )
                rebound.append(str(event["event_id"]))
            except ValueError as exc:
                warnings.append(f"event {event['event_id']}: {exc}")
        with self._uow_factory() as uow:
            migrated_memories = uow.person_memories.reconcile_identity(
                cluster_id,
                previous_person_id,
                person_id,
                actor,
                _datetime(self._now()),
            )
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "person_id": person_id,
            "previous_person_id": previous_person_id,
            "link_source": source,
            "link_confidence": confidence,
            "rebound_event_ids": rebound,
            "warnings": warnings,
            "migrated_memory_count": migrated_memories,
            **identity_changes,
        }

    def create_and_label(
        self, cluster_id: str, display_name: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        person = self.create_person(display_name)
        return {**self.label_cluster(cluster_id, person["person_id"], actor), "person": person}

    def merge(
        self,
        source_cluster_ids: tuple[str, ...],
        target_cluster_id: str,
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.merge_clusters(
                source_cluster_ids,
                target_cluster_id,
                actor,
                operation_id,
                _datetime(self._now()),
            )
        return {"operation_id": operation_id, "target_cluster_id": target_cluster_id}

    def split(
        self,
        cluster_id: str,
        speaker_track_ids: tuple[str, ...],
        actor: str = "desktop-user",
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        new_cluster_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.split_cluster(
                cluster_id,
                speaker_track_ids,
                new_cluster_id,
                "拆分后的未知说话人",
                actor,
                operation_id,
                _datetime(self._now()),
            )
        return {
            "operation_id": operation_id,
            "source_cluster_id": cluster_id,
            "new_cluster_id": new_cluster_id,
        }

    def ignore(
        self, cluster_id: str, reason: str, actor: str = "desktop-user"
    ) -> dict[str, Any]:
        operation_id = new_ulid()
        with self._uow_factory() as uow:
            uow.people.ignore_cluster(
                cluster_id, reason, actor, operation_id, _datetime(self._now())
            )
        return {"operation_id": operation_id, "cluster_id": cluster_id, "status": "ignored"}

    def undo(self, cluster_id: str, actor: str = "desktop-user") -> dict[str, Any]:
        operation_id = new_ulid()
        now = self._now()
        with self._uow_factory() as uow:
            current = uow.people.cluster_detail(cluster_id)
            reverted = uow.people.undo(
                cluster_id, actor, operation_id, _datetime(now)
            )
            if reverted["kind"] == "label":
                previous_person_id = reverted["payload"].get("previous_person_id")
                restored_identity = (
                    _identity_for_person_kind(uow.people.person_kind(previous_person_id))
                    if previous_person_id
                    else SelfIdentity.UNKNOWN
                )
                identity_changes = self._sync_cluster_identity(
                    uow,
                    cluster_id,
                    restored_identity,
                    actor,
                    now,
                )
            else:
                identity_changes = {
                    "updated_utterance_count": 0,
                    "preserved_manual_utterance_count": 0,
                }
            event_ids = tuple(reverted["payload"].get("rebound_event_ids", ()))
            events = uow.people.events_by_ids(event_ids)
        rebound: list[str] = []
        warnings: list[str] = []
        if reverted["kind"] == "label" and current.get("person_id"):
            replacement = reverted["payload"].get("previous_person_id") or cluster_id
            for event in events:
                with self._uow_factory() as uow:
                    evidence_ids = uow.people.cluster_evidence_ids(
                        cluster_id, str(event["session_id"])
                    )
                if not evidence_ids:
                    warnings.append(f"event {event['event_id']} has no same-session evidence")
                    continue
                try:
                    self._rebind_event(
                        event,
                        str(current["person_id"]),
                        str(replacement),
                        evidence_ids,
                        actor,
                    )
                    rebound.append(str(event["event_id"]))
                except ValueError as exc:
                    warnings.append(f"event {event['event_id']}: {exc}")
            with self._uow_factory() as uow:
                migrated_memories = uow.person_memories.reconcile_identity(
                    cluster_id,
                    str(current["person_id"]),
                    (
                        str(reverted["payload"]["previous_person_id"])
                        if reverted["payload"].get("previous_person_id")
                        else None
                    ),
                    actor,
                    _datetime(self._now()),
                )
        else:
            migrated_memories = 0
        return {
            "operation_id": operation_id,
            "cluster_id": cluster_id,
            "reverted_operation_id": reverted["operation_id"],
            "rebound_event_ids": rebound,
            "warnings": warnings,
            "migrated_memory_count": migrated_memories,
            **identity_changes,
        }

    @staticmethod
    def _sync_cluster_identity(
        uow: UnitOfWork,
        cluster_id: str,
        identity: SelfIdentity,
        actor: str,
        now: datetime,
    ) -> dict[str, int]:
        updated = 0
        preserved = 0
        for utterance_id in uow.people.cluster_evidence_ids(cluster_id):
            utterance = uow.evidence.get_utterance(utterance_id)
            if utterance.identity is identity:
                continue
            identity_corrections = tuple(
                correction
                for correction in uow.corrections.list_for_target(
                    "utterance", utterance_id
                )
                if "identity" in correction.patch
            )
            if identity_corrections:
                if not identity_corrections[-1].actor.startswith(
                    _CLUSTER_IDENTITY_ACTOR_PREFIX
                ):
                    preserved += 1
                    continue
            elif utterance.identity is not SelfIdentity.UNKNOWN:
                preserved += 1
                continue
            apply_utterance_correction(
                uow,
                CorrectUtteranceCommand(
                    utterance_id=utterance.utterance_id,
                    expected_revision=utterance.revision,
                    text=utterance.text,
                    actor=f"{_CLUSTER_IDENTITY_ACTOR_PREFIX}{actor}",
                    identity=identity,
                    change_identity=True,
                ),
                now,
            )
            updated += 1
        return {
            "updated_utterance_count": updated,
            "preserved_manual_utterance_count": preserved,
        }

    def _rebind_event(
        self,
        event: dict[str, Any],
        old_reference: str,
        person_id: str,
        evidence_ids: tuple[str, ...],
        actor: str,
    ) -> None:
        next_payload = _replace_reference(event["payload"], old_reference, person_id)
        if next_payload == event["payload"]:
            return
        submission = GenerationSubmission(
            layer=KnowledgeLayer.EVENT,
            producer="human-identity-correction",
            producer_version="3.4.0",
            model="none",
            prompt_version="person-reference-rebind-v1",
            extractor_version="person-reference-rebind-v1",
            input_scope={
                "source": "person_cluster_label",
                "cluster_id": old_reference,
                "person_id": person_id,
                "event_id": event["event_id"],
            },
            proposals=(
                (
                    ProposalKind.EVENT_OPERATION,
                    {
                        "operation": "update",
                        "event_id": event["event_id"],
                        "session_id": event["session_id"],
                        "event_kind": event["event_kind"],
                        "expected_revision": int(event["revision"]),
                        "patch": next_payload,
                    },
                    evidence_ids,
                ),
            ),
        )
        generation = self._knowledge.submit_generation(submission)
        proposal_id = str(generation["proposals"][0]["proposal_id"])
        self._knowledge.accept_proposal(proposal_id, actor)

    def _policy_dict(self) -> dict[str, float]:
        return {
            "anonymous_threshold": self._policy.anonymous_threshold,
            "person_suggestion_threshold": self._policy.person_suggestion_threshold,
            "minimum_margin": self._policy.minimum_margin,
        }


def _compatible(
    vector: tuple[float, ...], candidates: tuple[tuple[str, tuple[float, ...]], ...]
) -> tuple[tuple[str, tuple[float, ...]], ...]:
    return tuple(candidate for candidate in candidates if len(candidate[1]) == len(vector))


def _identity_policies(
    values: dict[str, dict[str, Any]],
) -> dict[str, PersonIdentityPolicy]:
    return {
        person_id: PersonIdentityPolicy(
            person_id=person_id,
            revision=int(value["revision"]),
            maturity_status=PersonIdentityMaturity(str(value["maturity_status"])),
            auto_match_enabled=bool(value["auto_match_enabled"]),
            suggest_threshold=float(value["suggest_threshold"]),
            auto_accept_threshold=float(value["auto_accept_threshold"]),
            minimum_margin=float(value["minimum_margin"]),
            minimum_quality=float(value["minimum_quality"]),
        )
        for person_id, value in values.items()
    }


def _identity_confidence(evidence: dict[str, Any]) -> float:
    raw = evidence.get("score")
    if not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(1.0, float(raw)))


def _replace_reference(value: Any, old: str, new: str) -> Any:
    if value == old:
        return new
    if isinstance(value, dict):
        return {key: _replace_reference(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_reference(item, old, new) for item in value]
    return value


def _datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("speaker identity timestamps require timezone information")
    return value.isoformat()


def _identity_for_person_kind(kind: str) -> SelfIdentity:
    if kind == PersonKind.SELF.value:
        return SelfIdentity.SELF
    if kind == PersonKind.KNOWN.value:
        return SelfIdentity.NOT_SELF
    raise ValueError(f"person kind cannot resolve utterance identity: {kind}")


__all__ = ["SpeakerIdentityService"]
