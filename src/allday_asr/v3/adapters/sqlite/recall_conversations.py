"""Bounded conversation selection over existing source IDs and reply edges."""

import json
import time
from allday_asr.v3.domain.source_identity import resolve_chat
from allday_asr.v3.domain.recall_conversation import participants
from allday_asr.v3.ports.chat_data import ChatDataError


def conversation_scopes(ids, scope, cancel, deadline):
    if not ids.chat_path.exists():
        return []
    people, manual = ids.people(), ids.manual()
    with ids.cache.connect() as c:
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(
            lambda: int(cancel.is_set() or time.monotonic() > deadline), 1000
        )
        # Indexed occurrence time, never ingested_at/captured_at. One anchor per
        # stable sender/conversation; no label-based conversation guesses.
        rows = c.execute(
            "SELECT body FROM search_index WHERE dataset=? AND sent_at>=? AND sent_at<=? "
            "GROUP BY platform,source,conversation,sender LIMIT 501",
            (ids.cache.get("dataset", ""), scope["sent_from"], scope["sent_to"]),
        ).fetchall()
    if len(rows) > 500:
        raise ChatDataError("IDENTITY_SCOPE_BUDGET_REACHED", 429)
    result = []
    for row in rows:
        record = json.loads(row[0])
        person, _ = resolve_chat(record, manual, people)
        if person != scope["person_id"] or scope["source"] not in {
            "all",
            record["platform"],
        }:
            continue
        if record.get("conversation_type") not in {"c2c", "group"}:
            continue
        selected = {
            k: record[k] for k in ("platform", "source_account_id", "conversation_id")
        }
        if selected not in result:
            result.append(selected)
    return result


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
