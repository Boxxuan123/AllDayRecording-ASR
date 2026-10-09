from __future__ import annotations
from .generation_context import generation_provenance

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
    def analyze(self, session_id: str, *, annotation_only: bool = False) -> dict[str, Any]:
        with self._uow_factory().reading() as uow:
            role = uow.people.session_dataset_role(session_id)
        if role is None:
            raise ValueError('session dataset role is unassigned')
        from allday_asr.v3.domain.identity_processing_policy import IdentityProcessingPolicy
        permission = IdentityProcessingPolicy.for_session(role, annotation_only=annotation_only)
        reservation = None
        if permission.recognition_enabled:
            with self._uow_factory() as uow:
                reservation = uow.people.begin_identity_prediction(session_id)
        if not permission.profile_learning_allowed:
            from .product_self_identity import infer_product_self
            product = (infer_product_self(self, session_id) if permission.recognition_enabled else
                       {"product_inference_executed": False, "product_skip_reason": "annotation_only"})
            shadow = getattr(self, 'blind_validation', None)
            if role == 'blind' and shadow is not None:
                try:
                    shadow.enqueue(session_id)
                    shadow.start()
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception('Shadow enqueue will retry independently')
            return {'session_id': session_id, 'status': 'succeeded', 'dataset_role': role,
                    'learning_excluded': True, 'recognition_enabled': permission.recognition_enabled,
                    'profile_learning_allowed': False, 'research_reservation': reservation, 'shadow_status': 'queued' if role == 'blind' else 'frozen_holdout', **product}
        if annotation_only:
            with self._uow_factory() as uow:
                uow.people.enqueue_samples(session_id)
            return {"session_id": session_id, "status": "queued"}
        matcher = self._self_identity_matcher
        matcher_snapshot = None
        if matcher is not None and hasattr(matcher, "freeze"):
            matcher = matcher.freeze(self._artifact_root)
            matcher_snapshot = matcher.status()
        with self._uow_factory() as uow:
            tracks = uow.people.analysis_inputs(session_id)
            uow.people.enqueue_samples(session_id)
            run_id = new_ulid()
            started_at = _datetime(self._now())
            uow.people.start_run(
                run_id,
                session_id,
                self._provider.model,
                self._provider.model_version,
                {**self._policy_dict(), "provenance": generation_provenance(
                    inputs={"session_id": session_id, "track_ids": [t.speaker_track_id for t in tracks]},
                    model={"id": self._provider.model, "revision": self._provider.model_version},
                    rules={"policy": self._policy_dict(), "profile_learning_allowed": True,
                           "self_matcher_snapshot": matcher_snapshot,
                           "identity_policies": uow.people.identity_policies(),
                           "person_vectors": uow.people.person_vectors(self._provider.model, self._provider.model_version),
                           "cluster_vectors": uow.people.cluster_vectors(self._provider.model, self._provider.model_version)})},
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
            match_inputs = []
            with self._uow_factory() as uow:
                self_person_id = uow.people.self_person_id()
                for index, embedding in enumerate(embeddings, start=1):
                    manual_cluster = uow.people.manual_track_cluster(embedding.speaker_track_id)
                    if manual_cluster is not None:
                        # Preserve the user's exact selected scope. Candidate generation
                        # does not accept the audio or relabel any other track.
                        uow.people.record_embedding(run_id=run_id, cluster_id=manual_cluster,
                            cluster_label="人工确认片段", create_cluster=False,
                            membership_id=new_ulid(), prototype_id=new_ulid(), operation_id=new_ulid(),
                            embedding=embedding, membership_confidence=1.0,
                            suggested_person_id=None, suggestion_confidence=None,
                            created_at=_datetime(self._now()), reuse_membership=True)
                        continue
                    self_match = (
                        matcher.match(embedding)
                        if self_person_id is not None
                        and matcher is not None
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
                    match_inputs.append({"speaker_track_id": embedding.speaker_track_id,
                        "anonymous_profiles": anonymous,
                        "self_match": self_match.evidence if self_match is not None else None})
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
                uow.audit.append("identity.match.snapshot", "system:initial-speaker-match",
                    "identity_match_snapshot", run_id, {"provenance": generation_provenance(
                        inputs={"run_id": run_id, "session_id": session_id, "matches": match_inputs},
                        model={"id": self._provider.model, "revision": self._provider.model_version},
                        rules={"policy": self._policy_dict(), "self_matcher_snapshot": matcher_snapshot})})
                uow.people.finish_run(
                    run_id, "succeeded", _datetime(self._now()), None
                )
            automatic = self.reconcile_self(session_id)
            known_people = self.rematch_existing(
                session_id, trigger="initial_analysis"
            )
            return {
                "recognition_enabled": True,
                "profile_learning_allowed": True,
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
        with self._uow_factory().reading() as uow:
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
    ) -> dict[str, Any]:
        """Re-evaluate anonymous prototypes without promoting any of them."""

        if trigger not in {
            "initial_analysis",
            "historical_rematch",
            "prototype_review",
            "policy_change",
        }:
            raise ValueError("speaker rematch trigger is invalid")
        def inputs(uow):
            return (uow.people.unlinked_cluster_embeddings(session_id),
                    uow.people.identity_policies(), uow.people.self_person_id(),
                    uow.people.person_vectors(self._provider.model, self._provider.model_version))

        with self._uow_factory().reading() as uow:
            snapshot = inputs(uow)
        candidates, raw_policies, self_person_id, people = snapshot
        policies = _identity_policies(raw_policies)
        evaluated = []
        for candidate in candidates:
            if (candidate["model"], candidate["model_version"]) != (self._provider.model, self._provider.model_version):
                continue
            vector = tuple(candidate["vector"])
            decision = layered_person_match(vector, _compatible(vector, people), policies,
                quality_score=float(candidate["quality_score"]))
            evaluated.append((candidate, decision))
        with self._uow_factory() as uow:
            if inputs(uow) != snapshot:
                raise ValueError("speaker matching inputs changed; retry with a fresh snapshot")
            snapshot_id = new_ulid()
            uow.audit.append("identity.match.snapshot", "system:known-person-match",
                "identity_match_snapshot", snapshot_id, {"provenance": generation_provenance(
                    inputs={"session_id": session_id, "candidates": candidates},
                    model={"id": self._provider.model, "revision": self._provider.model_version},
                    rules={"trigger": trigger, "strategy": _KNOWN_PERSON_POLICY_VERSION,
                           "identity_policies": raw_policies, "person_vectors": people})})
            grouped: dict[str, list[LayeredMatchDecision]] = {}
            created_at = _datetime(self._now())
            for candidate, decision in evaluated:
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
            "match_snapshot_id": snapshot_id,
            "rematched_prototype_count": len(candidates),
            "suggested_cluster_count": suggestion_count,
            "auto_identified_known_cluster_count": linked,
            "auto_known_identity_updated_utterance_count": updated,
        }
