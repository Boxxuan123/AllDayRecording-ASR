"""Constrain generated references to this round; consumer integrity checks remain authoritative."""

from copy import deepcopy
import json
from allday_asr.v3.domain.recall_event_time import full_explicit_dates


def event_time_schema(time, rows):
    """Explicit dates must be generated from full years actually present in text."""
    unknown, dated = deepcopy(time["anyOf"])
    dated["properties"]["basis"]["enum"] = ["relative", "inferred", "message"]
    groups = {}
    for row in rows:
        dates = tuple(full_explicit_dates(row.get("quote", "")))
        if dates:
            groups.setdefault(dates, []).append(row["id"])
    def explicit(dates, refs):
        branch = deepcopy(dated)
        branch["properties"]["basis"]["enum"] = ["explicit"]
        branch["properties"]["evidence_id"] = {"type": "string", "enum": refs}
        for key in ("first", "last"):
            branch["properties"][key] = {"type": "string", "enum": list(dates)}
        return branch
    branches = []
    for dates, refs in groups.items():
        branch = explicit(dates, refs)
        # A pathological quoted calendar must not exhaust the executor envelope.
        # Other date bases remain available, with the original strict validator.
        if len(json.dumps([*branches, branch], ensure_ascii=False).encode()) <= 3500:
            branches.append(branch)
    return {"anyOf": [unknown, dated, *branches]}


def evidence_schema(schema, rows):
    value = deepcopy(schema)
    refs = list(dict.fromkeys(row["id"] for row in rows))

    def reference():
        return {
            "type": "string",
            **({"enum": refs} if refs else {"pattern": r"^E[1-9]\d*$"}),
        }

    def visit(node):
        if isinstance(node, dict):
            props = node.get("properties", {})
            if "event_time" in props:
                props["event_time"] = event_time_schema(props["event_time"], rows)
            for key in ("citations", "evidence_ids"):
                if key in props and props[key].get("type") == "array":
                    # Replace the property rather than mutating shared STRING schema objects.
                    props[key] = {**props[key], "items": reference()}
            if "evidence_id" in props and "enum" not in props["evidence_id"]:
                props["evidence_id"] = reference()
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return value
