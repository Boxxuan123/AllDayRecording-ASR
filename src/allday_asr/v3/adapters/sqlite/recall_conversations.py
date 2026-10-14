"""Bounded conversation selection over existing source IDs and reply edges."""

import json
import time
from allday_asr.v3.domain.recall_conversation import participants
from allday_asr.v3.ports.chat_data import ChatDataError


def conversation_scopes(ids, scope, cancel, deadline):
    if scope.get("source_contacts"):
        return [
            {k: c[k] for k in ("platform", "source_account_id", "conversation_id")}
            for c in scope["source_contacts"]
        ]
    if not ids.chat_path.exists():
        return []
    people, manual = ids.people(), ids.manual()
    person = scope["person_id"]
    if person not in people:
        return []
    keys = [
        json.loads(k)
        for k, v in manual.items()
        if v == person or (v == "self" and people[person]["kind"] == "self")
    ]
    keys = [k for k in keys if scope["source"] in {"all", k[0]}]
    if not keys:
        return []
    if len(keys) > 20:
        raise ChatDataError("IDENTITY_SCOPE_BUDGET_REACHED", 429)
    result = []
    ranks = {}
    with ids.cache.connect() as c:
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(
            lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
        )
        for platform, source, sender in sorted(keys):
            # Existing index lookup in confirmed source namespaces. Only stable
            # conversation metadata is selected; query-day messages need not exist.
            rows = c.execute(
                "SELECT conversation,json_extract(body,'$.conversation_type') AS kind,max(sent_at) AS last_seen "
                "FROM search_index WHERE dataset=? AND platform=? AND source=? AND sender=? "
                "GROUP BY conversation,kind ORDER BY max(sent_at) DESC,conversation LIMIT 501",
                (ids.cache.get("dataset", ""), platform, source, sender),
            ).fetchall()
            if len(rows) > 500:
                raise ChatDataError("IDENTITY_SCOPE_BUDGET_REACHED", 429)
            for row in rows:
                if row["kind"] not in {"c2c", "group"} or not row["conversation"]:
                    continue
                selected = {
                    "platform": platform,
                    "source_account_id": source,
                    "conversation_id": row["conversation"],
                }
                key = (platform, source, row["conversation"])
                rank = (row["kind"] != "c2c", -(row["last_seen"] or 0), key)
                ranks[key] = min(rank, ranks.get(key, rank))
                if selected not in result:
                    result.append(selected)
                if len(result) > 500:
                    raise ChatDataError("IDENTITY_SCOPE_BUDGET_REACHED", 429)
    linked = [
        {k: c[k] for k in ("platform", "source_account_id", "conversation_id")}
        for c in scope.get("linked_contacts", [])
    ]
    return [s for s in linked if s not in result] + [
        s
        for s in sorted(
            result,
            key=lambda s: ranks[
                (s["platform"], s["source_account_id"], s["conversation_id"])
            ],
        )
    ]


def conversation_records(ids, scope, terms, limit, cancel, deadline):
    scopes = conversation_scopes(ids, scope, cancel, deadline)
    if not scopes:
        return [], False
    clauses = []
    params = [ids.cache.get("dataset", ""), scope["sent_from"], scope["sent_to"]]
    for selected in scopes:
        clauses.append("(platform=? AND source=? AND conversation=?)")
        params.extend(selected.values())
    with ids.cache.connect() as c:
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(
            lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
        )
        rows = c.execute(
            "SELECT body FROM search_index WHERE dataset=? AND sent_at>=? AND sent_at<=? AND ("
            + " OR ".join(clauses)
            + ") ORDER BY sent_at,id LIMIT 501",
            params,
        ).fetchall()
    records = participants(
        [json.loads(r[0]) for r in rows[:500]], ids.people(), ids.manual(), scope
    )
    if terms:
        # All genuine topic phrases are constraints. Context is added only after
        # selecting conversations containing an actual topic hit.
        keys = {
            tuple(r[k] for k in ("platform", "source_account_id", "conversation_id"))
            for r in records
            if all(t.casefold() in (r.get("text") or "").casefold() for t in terms)
        }
        records = [
            r
            for r in records
            if tuple(r[k] for k in ("platform", "source_account_id", "conversation_id"))
            in keys
        ]
    return records[:limit], len(rows) > 500 or len(records) > limit
