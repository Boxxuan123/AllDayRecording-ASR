"""Keep exact event candidates without disabling hard constraints or broad keyword filters."""

from allday_asr.v3.domain.recall_event_anchor import literal_anchor
from allday_asr.v3.domain.recall_investigation_scope import inside_root


class InvestigationRelevance:
    def branch_supported(self, target, action):
        actor = target.get("relation_account", target)
        support = [
            self.refs[e]
            for e in (
                action["evidence_ids"]
                or target.get("relation_evidence", [])
                or target.get("citations", [])
            )
            if e in self.refs
        ]
        return any(
            r.get("type") == "evidence"
            and r.get("source") == actor.get("platform")
            and r.get("source_account_id") == actor.get("source_account_id")
            and r.get("sender_account_id") == actor.get("sender_account_id")
            and r.get("source_is_from_self") is False
            for r in support
        )

    def remote_related(self, target, scope):
        from .recall_investigation_tools import contact_record_allowed
        from allday_asr.v3.ports.chat_data import ChatDataError
        from allday_asr.v3.domain.recall_investigation_scope import inside_root
        from copy import deepcopy

        self.verify_remote()
        remote = {
            k: target[k] for k in ("platform", "source_account_id", "sender_account_id")
        }
        remote.update(sent_from=scope["sent_from"], sent_to=scope["sent_to"])
        limit = min(20, 200 - self.result["metrics"]["source_records_read"])
        if limit <= 0:
            return []
        page = self.remote("/v1/messages", scope=remote, limit=limit)
        items = page.get("items")
        if not isinstance(items, list) or len(items) > limit:
            raise ChatDataError("INVALID_CHAT_PAGE", 502)
        self.result["metrics"]["source_records_read"] += len(items)
        contacts = {}
        for record in items:
            self.validate_native_record(record)
            if (
                any(
                    record.get(k) != remote[k]
                    for k in ("platform", "source_account_id", "sender_account_id")
                )
                or not remote["sent_from"]
                <= (record.get("sent_at") or -1)
                <= remote["sent_to"]
                or not contact_record_allowed(record, scope)
            ):
                raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            row = self.service.sources.evidence(record, "mac", "context")
            if not inside_root(
                row, self.root_scope, evidence=True
            ) or self.service.sources.changed([row]):
                continue
            matching = [
                c
                for c in self.directory
                if c["platform"] == row["source"]
                and c["source_account_id"] == row["source_account_id"]
                and c["conversation_id"] == row["conversation_id"]
            ]
            if matching:
                contact = deepcopy(matching[0])
            else:
                # A validated native message establishes its own authorized room even
                # when the unrelated directory pagination has not reached that room.
                import hashlib, json
                stable = [row.get("dataset"), row["source"], row["source_account_id"], row["conversation_id"]]
                contact = dict(key=hashlib.sha256(json.dumps(stable, ensure_ascii=False).encode()).hexdigest()[:32],
                    platform=row["source"], source_account_id=row["source_account_id"],
                    conversation_id=row["conversation_id"], conversation_type=row["conversation_type"],
                    sender_account_id=None, dataset=row.get("dataset"), basis="native_message_conversation",
                    display_name=row.get("conversation_display_name") or "来源会话",
                    names=[row.get("conversation_display_name") or "来源会话"])

            contact["relation_account"] = {
                k: target[k]
                for k in ("platform", "source_account_id", "sender_account_id")
            }
            contact["relation_evidence"] = [row["id"]]
            contact["relation_basis"] = "observed_same_source_account_speech"
            self.current_scope = scope
            self.add([row])
            contacts[contact["key"]] = contact
        if page.get("has_more"):
            self.note("发言账号还有后续页；关系候选仅覆盖本次取回的原文。")
        return [self.ref("contact", c, c["key"]) for c in contacts.values()]

    def candidate_terms(self, terms):
        # A literal event name/identifier must also be present in the search terms.
        anchor = next(
            (
                a
                for a in self.anchors
                if any(
                    t.casefold() in a.casefold() or a.casefold() in t.casefold()
                    for t in terms
                )
            ),
            None,
        )
        return [anchor] if anchor else terms

    def topic_rows(self, rows, terms):
        kept = []
        invalid = self.service.sources.changed(rows)
        for row in rows:
            self.flow.record("candidate_seen", row, "scoped_topic_candidate")
            if row["id"] in invalid or not inside_root(
                row, self.root_scope, evidence=True
            ):
                self.flow.record("hard_rejected", row, "topic_scope_or_integrity")
                continue
            self.flow.record(
                "application_accepted", row, "hard_scope_and_integrity_valid"
            )
            candidates = self.candidate_terms(terms)
            if (
                candidates
                and candidates[0] in self.anchors
                and not literal_anchor(row["quote"], candidates)
            ):
                self.flow.record("soft_rejected", row, "different_literal_event")
            elif not self.plan.get("exact") or all(t.casefold() in row["quote"].casefold() for t in terms):
                kept.append(row)
            elif literal_anchor(row["quote"], self.anchors):
                row = {
                    **row,
                    "retrieval_basis": "literal_event_anchor_candidate",
                    "relation_ids": [],
                    "relevance_confirmed": False,
                }
                self.flow.record(
                    "soft_terms_not_matched", row, "kept_literal_event_anchor"
                )
                kept.append(row)
            else:
                self.flow.record(
                    "soft_rejected", row, "initial_keywords_without_relationship"
                )
        return kept
