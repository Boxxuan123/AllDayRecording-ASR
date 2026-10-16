"""Read-only relationship leads from existing SQLite; no inferred graph writes."""

import json
from allday_asr.v3.domain.recall_read_budget import charge, scan_limit
import time
from .recall_state import bounded_sql
from .recall_directory import entry


class RecallRelations:
    @bounded_sql
    def related_conversations(self, account, scope, cancel, deadline):
        ids = self.identities
        if not ids.chat_path.exists():
            return [], False
        with ids.cache.connect() as c:
            c.execute("PRAGMA query_only=ON")
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() >= deadline), 1000
            )
            from allday_asr.v3.domain.chat_data import packed

            stable = [
                account[k]
                for k in ("platform", "source_account_id", "sender_account_id")
            ]
            manual = ids.manual()
            person = manual.get(packed(stable))
            accounts = [stable]
            if person in ids.people():
                accounts.extend(
                    json.loads(k)
                    for k, v in manual.items()
                    if v == person
                    and json.loads(k) != stable
                    and scope["source"] in {"all", json.loads(k)[0]}
                )
            rows = []
            for platform, source, sender in accounts[:20]:
                if len(rows) >= 101:
                    break
                restrictions, params = [], []
                for field, columns in (
                    ("source_accounts", ("platform", "source")),
                    ("source_contacts", ("platform", "source", "conversation")),
                ):
                    if scope.get(field):
                        restrictions.append(
                            "("
                            + " OR ".join(
                                "(" + " AND ".join(col + "=?" for col in columns) + ")"
                                for _ in scope[field]
                            )
                            + ")"
                        )
                        params.extend(
                            contact[k]
                            for contact in scope[field]
                            for k in (
                                "platform",
                                "source_account_id",
                                "conversation_id",
                            )[: len(columns)]
                        )
                if scope["source"] not in {"all", platform}:
                    continue
                extra = " AND " + " AND ".join(restrictions) if restrictions else ""
                rows.extend(
                    c.execute(
                        "SELECT body FROM search_index WHERE dataset=? AND platform=? AND source=? AND sender=? "
                        "AND sent_at>=? AND sent_at<=?"
                        + extra
                        + " ORDER BY sent_at DESC,id LIMIT ?",
                        (
                            self.dataset(),
                            platform,
                            source,
                            sender,
                            scope["sent_from"],
                            scope["sent_to"],
                            *params,
                            scan_limit(101 - len(rows)),
                        ),
                    ).fetchall()
                )
        bodies = [charge(row[0]) for row in rows]
        results = []
        seen = set()
        for body in bodies[:100]:
            r = json.loads(body)
            key = (r["platform"], r["source_account_id"], r["conversation_id"])
            if key in seen:
                continue
            seen.add(key)
            contact = entry(
                {
                    **r,
                    "display_name": r.get("conversation_display_name")
                    or (
                        "已授权群聊"
                        if r.get("conversation_type") == "group"
                        else "已授权私聊"
                    ),
                    "peer_account_id": r["sender_account_id"]
                    if r.get("conversation_type") == "c2c"
                    else None,
                },
                self.dataset(),
            )
            if contact:
                results.append(
                    {
                        "contact": contact,
                        "evidence": self.evidence(r, "cache", "context"),
                        "relation_basis": "confirmed_person_link"
                        if r["platform"] != account["platform"]
                        or r["source_account_id"] != account["source_account_id"]
                        else "same_scoped_account_speech",
                    }
                )
            if len(results) == 20:
                break
        return results, len(rows) > 100 or len(results) >= 20 or len(accounts) > 20

    @bounded_sql
    def existing_memory(self, scope, terms, cancel, deadline):
        # Memory text is a lead, never a primary citation or an automatic fact.
        if scope["source"] in {"qq", "wechat"}:
            return []
        with self.identities.database.read() as c:
            c.execute("PRAGMA busy_timeout=1000")
            c.set_progress_handler(
                lambda: int(cancel.is_set() or time.monotonic() >= deadline), 1000
            )
            clauses = [
                "m.status='active'",
                "m.revision=(SELECT max(n.revision) FROM person_memory_entries n WHERE n.memory_id=m.memory_id)",
                "unixepoch(m.valid_from)<=?",
                "(m.valid_until IS NULL OR unixepoch(m.valid_until)>=?)",
            ]
            args = [scope["sent_to"], scope["sent_from"]]
            if scope["person_id"]:
                clauses.append("m.person_id=?")
                args.append(scope["person_id"])
            elif not terms:
                return []
            for term in terms:
                clauses.append("instr(lower(m.summary),lower(?))>0")
                args.append(term)
            rows = c.execute(
                "SELECT m.memory_id,m.revision,m.person_id,m.summary,m.source,m.confirmation_status,m.event_id "
                "FROM person_memory_entries m WHERE "
                + " AND ".join(clauses)
                + " ORDER BY m.created_at DESC,m.memory_id LIMIT 20",
                args,
            ).fetchall()
            events = []
            if terms and not scope["person_id"] and scope["source"] == "all":
                clauses = [
                    "x.revision=(SELECT max(revision) FROM cross_day_index_revisions)",
                    "unixepoch(json_extract(j.value,'$.occurred_at'))>=?",
                    "unixepoch(json_extract(j.value,'$.occurred_at'))<=?",
                ]
                args = [scope["sent_from"], scope["sent_to"]]
                for term in terms:
                    clauses.append(
                        "instr(lower(coalesce(json_extract(j.value,'$.title'),'') || ' ' || coalesce(json_extract(j.value,'$.summary'),'')),lower(?))>0"
                    )
                    args.append(term)
                events = c.execute(
                    "SELECT j.value FROM cross_day_index_revisions x, "
                    "json_each(x.value_json,'$.discussions') j WHERE "
                    + " AND ".join(clauses)
                    + " LIMIT 10",
                    args,
                ).fetchall()
        result = [
            {
                **dict(r),
                "summary": r["summary"][:700],
                "basis": "existing_memory_lead_not_primary_evidence",
            }
            for r in rows
        ]
        for row in events:
            item = json.loads(row[0])
            result.append(
                {
                    "memory_id": "discussion:" + str(item.get("id", "")),
                    "summary": str(item.get("summary", item.get("title", "")))[:700],
                    "occurred_at": item.get("occurred_at"),
                    "basis": "existing_event_lead_not_primary_evidence",
                    "evidence_status": item.get("evidence_status", "unknown"),
                }
            )
        return result[:20]
