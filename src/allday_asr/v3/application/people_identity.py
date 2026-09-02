from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from allday_asr.v3.domain.people import (
    LayeredMatchDecision,
    SpeakerEmbedding,
    SpeakerMatchTier,
    conservative_match,
    layered_person_match,
)

from .people_support import (
    _KNOWN_PERSON_POLICY_VERSION,
    _compatible,
    _datetime,
    _identity_confidence,
    _identity_policies,
)


class PeopleIdentityMixin:
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
