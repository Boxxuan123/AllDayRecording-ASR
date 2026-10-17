"""Constrain generated references to this round; consumer integrity checks remain authoritative."""

from copy import deepcopy


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
            for key in ("citations", "evidence_ids"):
                if key in props and props[key].get("type") == "array":
                    # Replace the property rather than mutating shared STRING schema objects.
                    props[key] = {**props[key], "items": reference()}
            if "evidence_id" in props and props["evidence_id"].get("enum") != [""]:
                props["evidence_id"] = reference()
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return value
