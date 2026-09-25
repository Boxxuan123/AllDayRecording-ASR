"""User-facing state follows actual matching predicates, not save receipts."""

from allday_asr.v3.domain.sound_kind import sound_uses


def annotation_status(service, utterance_ids):
    if not isinstance(utterance_ids, list) or not 1 <= len(utterance_ids) <= 100:
        raise ValueError("请选择 1 至 100 个片段查询状态")
    result = []
    with service._uow_factory() as uow:
        for uid in utterance_ids:
            row = uow.evidence.get_utterance(uid)
            evidence = uow.evidence.annotation_projection_evidence(row)
            uncertain = "person" in evidence.get(
                "annotation_outdated", []
            ) or "person" in evidence.get("annotation_review", {}).get("dimensions", {})
            person = evidence.get("person_annotation") if not uncertain else None
            pid = person.get("person_id") if person else None
            kind = uow.people.person_kind(pid) if pid else None
            policy = uow.people.identity_policy(pid) if kind == "known" else None
            samples = uow.people.annotation_samples(uid)
            for sample in samples:
                compatible = (sample["model"], sample["model_version"]) == (
                    service._provider.model,
                    service._provider.model_version,
                )
                sample_policy = (
                    uow.people.identity_policy(sample["person_id"])
                    if sample["person_id"]
                    and uow.people.person_kind(sample["person_id"]) == "known"
                    else policy
                )
                quality = (
                    sample_policy is not None
                    and sample["quality_score"] >= sample_policy["minimum_quality"]
                )
                sample["matching_eligible"] = bool(
                    sample["source_usable"]
                    and compatible
                    and quality
                    and sample["current_link"]
                    and sample["human_confirmed"]
                    and sample["status"] == "accepted"
                    and sample["review_decision"] in {None, "confirmed"}
                    and sample["person_id"]
                    and uow.people.person_kind(sample["person_id"]) == "known"
                )
                sample["reason"] = (
                    "source_invalidated"
                    if not sample["source_usable"]
                    else "incompatible_model"
                    if not compatible
                    else "self_uses_separate_calibrated_library"
                    if kind == "self"
                    else "below_person_quality_policy"
                    if not quality
                    else "available_to_matching"
                    if sample["matching_eligible"]
                    else sample["review_decision"] or "awaiting_sample_review"
                )
            job = uow.people.sample_job(row.session_id)
            eligible = any(s["matching_eligible"] for s in samples)
            candidates = [s for s in samples if s["reason"] == "awaiting_sample_review"]
            accepted = [s for s in samples if s["status"] == "accepted"]
            state = job["status"]
            reason = job.get("reason", "")
            if eligible:
                state, reason = "matching", "available_to_matching"
            elif candidates:
                state, reason = "candidate", "awaiting_sample_review"
            elif state not in {"queued", "running", "retryable"}:
                if accepted:
                    state, reason = "accepted_unusable", accepted[-1]["reason"]
                elif samples:
                    state, reason = "insufficient", samples[-1]["reason"]
            if kind == "self":
                reason = "self_uses_separate_calibrated_library"
            result.append(
                {
                    "utterance_id": uid,
                    "revision": row.revision,
                    "fact_status": "conflict"
                    if uncertain
                    else "saved"
                    if person
                    else "unassigned",
                    "person_annotation": person,
                    "uses": sound_uses(evidence),
                    "samples": samples,
                    "processing_status": state,
                    "processing_reason": reason,
                    "matching_model": service._provider.model,
                    "matching_model_version": service._provider.model_version,
                    "note": "采纳状态与自动匹配策略独立；本人沿用独立注册库。",
                }
            )
    return {"items": result}
