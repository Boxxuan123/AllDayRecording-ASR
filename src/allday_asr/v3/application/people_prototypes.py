from __future__ import annotations

from typing import Any

from allday_asr.v3.domain.ids import new_ulid, stable_ulid
from allday_asr.v3.domain.people import PersonIdentityMaturity, PersonKind, cosine_similarity
from allday_asr.v3.domain.processing import SpeakerTrack

from .people_support import _KNOWN_PERSON_POLICY_VERSION, _datetime


class PeoplePrototypeMixin:
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
            candidate = (uow.people.prototype_retraction_target(prototype_id, person_id)
                         if decision == 'retracted' else uow.people.prototype_candidate(prototype_id))
            if decision == "confirmed":
                if (candidate['model'], candidate['model_version']) != (self._provider.model, self._provider.model_version):
                    raise ValueError('样本模型版本已变化，不能采纳旧版本')
                policy = uow.people.identity_policy(person_id)
                if float(candidate["quality_score"]) < float(policy["minimum_quality"]):
                    raise ValueError("样本未达到此人物当前的质量策略，不能采纳")
            current_review = uow.people.latest_prototype_review(
                prototype_id, person_id
            )
            cluster = (uow.people.cluster_detail(str(candidate["active_cluster_id"]))
                       if decision == 'confirmed' else None)
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
            if decision == 'confirmed':
                latest = uow.people.prototype_candidate(prototype_id)
                if (latest['model'], latest['model_version']) != (self._provider.model, self._provider.model_version):
                    raise ValueError('样本模型版本已变化，不能采纳旧版本')
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
    def _policy_dict(self) -> dict[str, float]:
        return {
            "anonymous_threshold": self._policy.anonymous_threshold,
            "person_suggestion_threshold": self._policy.person_suggestion_threshold,
            "minimum_margin": self._policy.minimum_margin,
        }
