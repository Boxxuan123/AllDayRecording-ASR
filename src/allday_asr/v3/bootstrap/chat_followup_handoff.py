"""Install captured local candidates after normal startup; never starts a model."""

import json
from allday_asr.v3.domain.chat_followups import conversation_key
from allday_asr.v3.domain.chat_data import packed
from allday_asr.v3.ports.chat_data import ChatDataError


def import_handoff(controller):
    path = controller.root / "handoff-candidates.json"
    if not path.exists():
        return {"state": "none"}
    applied = duplicates = 0
    try:
        if path.stat().st_size > 2_000_000:
            raise ChatDataError("FOLLOWUP_HANDOFF_SIZE", 413)
        value = json.loads(path.read_text(encoding="utf8"))
        dataset = value["dataset"]
        records = value["records"]
        batches = value["batches"]
        if (
            not isinstance(records, list)
            or len(records) > 1000
            or not isinstance(batches, list)
            or len(batches) > 12
        ):
            raise ChatDataError("INVALID_FOLLOWUP_HANDOFF", 400)
        if controller.chat.cache.get("dataset") != dataset:
            raise ChatDataError("FOLLOWUP_SOURCE_CHANGED", 409)
        scopes, tz = controller.scopes()
        allowed = {
            packed(
                [s.get(k) for k in ("platform", "source_account_id", "conversation_id")]
            )
            for s in scopes
        }
        if not allowed or any(conversation_key(r) not in allowed for r in records):
            raise ChatDataError("SCOPE_MISMATCH", 409)
        by_id = {r["record_id"]: r for r in records}
        # Only evidence records are cached; receipt/index registration remains atomic.
        controller.chat.cache.receive(dataset, records)
        for batch in batches:
            for c in batch["items"]:
                if c.get("action") != "create":
                    raise ChatDataError("INVALID_FOLLOWUP_HANDOFF_ACTION", 400)
                evidence = [by_id[x["record_id"]] for x in c["evidence"]]
                result = controller.service.apply(
                    c,
                    evidence,
                    dataset,
                    controller.links(),
                    timezone_name=tz,
                    provenance=batch.get("provenance"),
                )
                applied += result["effect"] == "applied"
                duplicates += result["effect"] == "duplicate"
        return {
            "state": "imported",
            "applied": applied,
            "duplicate": duplicates,
            "model_calls": 0,
            "acceptance": {
                k: v
                for k, v in value.get("acceptance", {}).items()
                if k
                in {
                    "unique_messages",
                    "historical_messages",
                    "recent_messages",
                    "calls",
                    "validated_items",
                    "coverage_complete",
                }
                and type(v) in {int, bool}
            },
        }
    except (ChatDataError, ValueError, TypeError, KeyError, OSError) as exc:
        return {
            "state": "pending",
            "applied": applied,
            "duplicate": duplicates,
            "error": getattr(exc, "code", type(exc).__name__),
            "model_calls": 0,
        }
