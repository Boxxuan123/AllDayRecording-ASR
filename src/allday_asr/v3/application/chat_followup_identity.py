"""Reinterpret only captured candidate evidence, retaining manual and review decisions."""

import json
from allday_asr.v3.domain.chat_followups import validate_candidate
from allday_asr.v3.domain.chat_admission import evidence_digest
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError


def rebuild_identity(service, cache):
    dataset = cache.get("dataset")
    links = cache.identity_links()["links"]
    with service.uow_factory().reading() as uow:
        rows = uow.followups.identity_candidates(dataset)
    if len(rows) > 5000:
        raise ChatDataError("FOLLOWUP_IDENTITY_BUDGET", 429)
    counts = {
        "candidates": len(rows),
        "reinterpreted": 0,
        "evidence_unavailable": 0,
        "model_calls": 0,
        "effects": {},
    }
    for row in rows:
        old = json.loads(row["value_json"])
        records = [cache.record(e["record_id"]) for e in old["evidence"]]
        if any(x is None for x in records):
            counts["evidence_unavailable"] += 1
            continue
        try:
            value = validate_candidate(
                old,
                records,
                dataset,
                links,
                int(service.now().timestamp()),
                old.get("timezone"),
            )
            if evidence_digest(value) != row["evidence_digest"]:
                raise ChatDataError("FOLLOWUP_EVIDENCE_CHANGED", 409)
            # No title/status/time/review override: this is a derived identity audit projection.
            updated = dict(old)
            for key in [
                "actor_person_id",
                "actor_is_from_self",
                "identity_conflict",
                "category",
                "interpretation_key",
                "evidence",
            ]:
                updated[key] = value[key]
            if updated != old:
                row["value_json"] = packed(updated)
                with service.uow_factory() as uow:
                    uow.followups.save_admission(row)
            result = service.apply(
                old,
                records,
                dataset,
                links,
                timezone_name=old.get("timezone"),
                provenance=old.get("provenance"),
            )
            counts["effects"][result["effect"]] = (
                counts["effects"].get(result["effect"], 0) + 1
            )
            counts["reinterpreted"] += 1
        except ChatDataError:
            counts["evidence_unavailable"] += 1
    return counts


def hydrate_identity(chat, cache):
    """Read-time identity for old cards; manual title/status/category are immutable here."""
    from hashlib import sha256
    from allday_asr.v3.domain.chat_identity import checked_identity, actor_identity

    prior = chat.get("actor_is_from_self")
    for evidence in chat.get("evidence", []):
        record = cache.record(evidence["record_id"])
        valid = (
            record
            and record["record_revision"] == evidence["record_revision"]
            and all(
                record[k] == evidence[k]
                for k in ("platform", "source_account_id", "conversation_id")
            )
        )
        if valid and evidence.get("raw_text_sha256"):
            valid = (
                sha256((record.get("text") or "").encode()).hexdigest()
                == evidence["raw_text_sha256"]
            )
        evidence.update(
            checked_identity(record)
            if valid
            else {
                "is_from_self": None,
                "self_identity_basis": "evidence_revision_unavailable",
            }
        )
    anchor = next(
        (
            r
            for r in chat.get("evidence", [])
            if r["record_id"] == chat.get("anchor_id")
        ),
        None,
    )
    if anchor:
        _, _, flag, conflict = actor_identity(
            anchor,
            chat.get("actor_account_id"),
            chat["evidence"],
            cache.identity_links()["links"],
        )
        chat["actor_is_from_self"] = flag
        chat["identity_conflict"] = conflict or (
            prior is not None and flag is not None and prior != flag
        )
