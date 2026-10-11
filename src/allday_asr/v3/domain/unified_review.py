"""Grounding checks for read-only review output; references use discriminated sources."""

SECTIONS = {
    "activity": "今天做了什么、讨论了什么",
    "followup": "承诺、进展与待跟进",
    "people": "值得记住的人物互动",
}


def validate_review(value, inputs):
    if not isinstance(value, dict) or set(value) != {
        "headline",
        "claims",
        "limitations",
    }:
        raise ValueError("invalid review output")
    if (
        not isinstance(value["headline"], str)
        or not isinstance(value["claims"], list)
        or len(value["claims"]) > 24
    ):
        raise ValueError("invalid review prose")
    if not isinstance(value["limitations"], list) or any(
        not isinstance(x, str) for x in value["limitations"]
    ):
        raise ValueError("invalid review limitations")
    by_id = {e["evidence_id"]: e for e in inputs["evidence"]}
    tasks = {t["event_id"]: t for t in inputs["followups"]}
    events = set(tasks)
    selected = {}
    for claim in value["claims"]:
        if set(claim) != {
            "section",
            "text",
            "certainty",
            "perspective",
            "evidence",
            "event_ids",
        }:
            raise ValueError("invalid claim fields")
        if (
            claim["section"] not in SECTIONS
            or claim["certainty"] not in {"fact", "uncertain"}
            or claim["perspective"] not in {"self", "other", "observed", "unknown"}
            or not isinstance(claim["text"], str)
            or not claim["text"].strip()
        ):
            raise ValueError("invalid claim semantics")
        if not isinstance(claim["evidence"], list) or not claim["evidence"]:
            raise ValueError("claim requires evidence")
        refs = []
        for ref in claim["evidence"]:
            if set(ref) != {"evidence_id", "quote"}:
                raise ValueError("invalid evidence fields")
            e = by_id.get(ref["evidence_id"])
            if (
                not e
                or not isinstance(ref["quote"], str)
                or not ref["quote"].strip()
                or ref["quote"] not in e["text"]
            ):
                raise ValueError("invented or unavailable quotation")
            refs.append(e)
            selected[e["evidence_id"]] = e
        if claim["perspective"] == "self" and not any(
            e.get("is_from_self") is True for e in refs
        ):
            raise ValueError("self claim needs identified self speech")
        if claim["perspective"] == "unknown" and claim["certainty"] != "uncertain":
            raise ValueError("unknown identity must remain uncertain")
        if not isinstance(claim["event_ids"], list) or set(claim["event_ids"]) - events:
            raise ValueError("invented associated task")
        supported = {i for e in refs for i in e.get("event_ids", [])}
        if set(claim["event_ids"]) - supported:
            raise ValueError("task not connected to cited source")
        if len({e["source_type"] for e in refs}) > 1:
            common = set(refs[0].get("event_ids", []))
            for e in refs[1:]:
                common &= set(e.get("event_ids", []))
            if not common:
                raise ValueError(
                    "cross-source merging needs a stable existing association"
                )
        if claim["section"] == "followup" and claim["perspective"] == "self":
            if all(
                e.get("conversation_type") == "group"
                and e.get("relevance") != "admitted"
                for e in refs
            ):
                raise ValueError("group interaction alone is not a personal task")
    return {**value, "evidence": list(selected.values())}


def admit_review(value, inputs):
    """Keep individually grounded conclusions; unsupported output never blocks valid evidence."""
    from copy import deepcopy

    value = deepcopy(value)
    validate_review({**value, "claims": []}, inputs)
    admitted, rejected, limitations = [], [], list(value["limitations"])
    for index, claim in enumerate(value["claims"]):
        if claim.get("perspective") == "unknown":
            claim["certainty"] = "uncertain"
        # Lack-of-evidence belongs to scope limitations, not an evidenced daily fact.
        if (
            claim.get("section") == "followup"
            and claim.get("perspective") == "unknown"
            and not claim.get("event_ids")
            and any(
                word in claim.get("text", "")
                for word in ("没有足够依据", "未见", "未能确认")
            )
        ):
            limitations.append(claim["text"])
            rejected.append({"index": index, "reason": "absence_is_scope_limitation"})
            continue
        try:
            validate_review({**value, "claims": [claim]}, inputs)
        except ValueError as exc:
            rejected.append({"index": index, "reason": str(exc)})
        else:
            admitted.append(claim)
    if not admitted:
        raise ValueError("no grounded review conclusions")
    if rejected:
        limitations.append(
            f"本地核验剔除了 {len(rejected)} 条缺少充分依据或混合了未关联来源的结论；不据此推断事项已完成或不存在。"
        )
    grounded = validate_review(
        {**value, "claims": admitted, "limitations": list(dict.fromkeys(limitations))},
        inputs,
    )
    grounded["validation_rejections"] = rejected
    return grounded
