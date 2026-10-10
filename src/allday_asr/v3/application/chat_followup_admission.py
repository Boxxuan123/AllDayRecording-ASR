"""Evidence-bound local review and safe withdrawal on the existing Event UOW."""

from hashlib import sha256
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.domain.chat_admission import POLICY, evidence_digest
from allday_asr.v3.domain.chat_followups import validate_candidate
from allday_asr.v3.domain.chat_projection import semantic_value
from allday_asr.v3.domain.knowledge import EventOperationKind, EventStatus
from allday_asr.v3.ports.chat_data import ChatDataError


class ChatRelevanceAdmission:
    @staticmethod
    def _admission(value, decision, origin, review_ref):
        return {
            "source_key": value["source_key"],
            "dataset": value["dataset"],
            "conversation_key": value["conversation_key"],
            "evidence_digest": evidence_digest(value),
            "decision": decision,
            "origin": origin,
            "review_ref": review_ref,
            "value_json": packed(semantic_value(value)),
        }

    def review_relevance(
        self,
        candidate,
        records,
        dataset,
        links,
        *,
        binding,
        decision,
        origin,
        review_ref,
        timezone_name=None,
        reason="",
    ):
        """Local reviewed selection, not model output; resolves live source inside the UOW.

        Binding contains hashes/namespace, never an isolated event ID or revision.
        """
        if not isinstance(reason, str) or len(reason) > 2000:
            raise ChatDataError("INVALID_RELEVANCE_REVIEW", 422)
        if (
            decision not in {"text_only", "conditional", "task", "cared"}
            or origin
            not in {
                "human_denial",
                "human_selection",
                "review_policy",
                "conditional_interest",
            }
            or not isinstance(review_ref, str)
            or not review_ref
        ):
            raise ChatDataError("INVALID_RELEVANCE_REVIEW", 422)
        if origin == "human_denial" and decision != "text_only":
            raise ChatDataError("INVALID_RELEVANCE_REVIEW", 422)
        if decision in {"task", "cared"} and origin != "human_selection":
            raise ChatDataError("INVALID_RELEVANCE_REVIEW", 422)
        value = validate_candidate(
            candidate,
            records,
            dataset,
            links,
            int(self.now().timestamp()),
            timezone_name,
        )
        if binding != {
            k: value[k] for k in ("source_key", "dataset", "conversation_key")
        } | {"evidence_digest": evidence_digest(value)}:
            raise ChatDataError("RELEVANCE_BINDING_CHANGED", 409)
        value["review_reason"] = reason
        with self.uow_factory() as uow:
            source = uow.followups.source(value["source_key"])
            if source:
                if (
                    source["dataset"] != dataset
                    or source["conversation_key"] != value["conversation_key"]
                ):
                    raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
                current = uow.knowledge.get_event(source["event_id"])
                stored = current.payload.get("chat_followup", {})
                if evidence_digest(stored) != evidence_digest(value):
                    raise ChatDataError("RELEVANCE_EVIDENCE_CHANGED", 409)
                if self._protected(uow, source, current):
                    return {
                        "effect": "human_conflict",
                        "source_key": value["source_key"],
                        "event_id": current.event_id,
                    }
            prior = uow.followups.admission(value["source_key"], dataset)
            if (
                prior
                and prior["origin"] in {"human_denial", "human_selection"}
                and origin not in {"human_denial", "human_selection"}
            ):
                return {"effect": "human_conflict", "source_key": value["source_key"]}
            review = self._admission(value, decision, origin, review_ref)
            uow.followups.save_admission(review)
            if prior != review:
                uow.audit.append(
                    "chat.relevance.reviewed",
                    "local-review:" + origin,
                    "chat_source",
                    value["source_key"],
                    {"decision": decision, "review_ref": review_ref},
                )
            if decision in {"text_only", "conditional"}:
                return self._exclude(uow, source, value, origin)
            return {"effect": "reviewed", "source_key": value["source_key"]}

    @staticmethod
    def _protected(uow, source, current):
        return (
            source["human_override"]
            or source["ignored"]
            or current.session_id is not None
            or current.payload.get("chat_followup", {}).get("linked_recording_event_id")
            or uow.reminders.get_schedule(current.event_id) is not None
        )

    def _exclude(self, uow, source, value, reason):
        if source:
            current = uow.knowledge.get_event(source["event_id"])
            if self._protected(uow, source, current):
                return {
                    "effect": "human_conflict",
                    "event_id": current.event_id,
                    "source_key": value["source_key"],
                }
            chat = dict(current.payload.get("chat_followup", {}))
            if evidence_digest(chat) != evidence_digest(value):
                return {
                    "effect": "evidence_conflict",
                    "source_key": value["source_key"],
                    "event_id": current.event_id,
                }
            chat["evidence"] = [
                {
                    **r,
                    "raw_text_sha256": r.get("raw_text_sha256")
                    or sha256((r.get("text") or "").encode()).hexdigest(),
                }
                for r in chat.get("evidence", [])
            ]
            if chat.get("personal_scope") is not False:
                chat.update(
                    personal_scope=False,
                    admission={
                        "decision": "text_only",
                        "reason": reason,
                        "policy": POLICY,
                    },
                )
                self._append(
                    uow,
                    current,
                    {"chat_followup": semantic_value(chat)},
                    EventOperationKind.CANCEL
                    if current.status is EventStatus.ACTIVE
                    else EventOperationKind.UPDATE,
                    "system:chat-relevance",
                )
        return {
            "effect": "excluded",
            "source_key": value["source_key"],
            "reason": reason,
        }
