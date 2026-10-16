"""Namespace-scoped contiguous candidate blocks; no semantic truth from adjacency."""

def conversation(row):
    return tuple(row.get(k) for k in ("source", "source_account_id", "conversation_id"))


def blocks(rows, gap=1800):
    groups = {}
    for row in rows:
        groups.setdefault(conversation(row), []).append(row)
    result = []
    for key, group in sorted(groups.items(), key=lambda pair: repr(pair[0])):
        chunk = []
        for row in sorted(group, key=lambda r: (r.get("sent_at") or 0, r["id"])):
            if chunk and (row.get("sent_at") or 0) - (chunk[-1].get("sent_at") or 0) > gap:
                result.append((key, chunk)); chunk = []
            chunk.append(row)
        if chunk:
            result.append((key, chunk))
    return result


def register_step(trace, refs, registry, before, evidence):
    trace["record_refs"] = list(dict.fromkeys(
        [r["id"] for r in evidence if (r["id"], r.get("revision")) not in before]
        + [ref for ref in refs if registry.get(ref, {}).get("type") == "evidence"]))
    trace["conversation_refs"] = [r for r in refs if registry.get(r, {}).get("type") == "contact"]
    trace["account_refs"] = [r for r in refs if registry.get(r, {}).get("type") == "account"]
    trace["memory_refs"] = [r for r in refs if registry.get(r, {}).get("type") == "memory"]
    # Legacy readers can use hits, but its meaning is now exclusively record references.
    trace["hits"] = trace["record_refs"]
    rows = [r for r in evidence if r["id"] in trace["record_refs"]]
    trace["context_blocks"] = [{"namespace": list(key), "record_refs": [r["id"] for r in group],
        "sent_from": group[0]["sent_at"], "sent_to": group[-1]["sent_at"]}
        for key, group in blocks(rows)]
