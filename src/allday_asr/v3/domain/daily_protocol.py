"""Equivalence-only normalization of explicitly set-valued protocol fields."""

import copy
import json

CANONICALIZATION_VERSION = "daily-protocol-canonicalization.1"


class NonCanonicalizableProtocolError(ValueError):
    pass


def canonicalize_response(payload):
    original = copy.deepcopy(payload)
    value = copy.deepcopy(payload)
    operations = []

    def dedup(obj, field, path, operation="DEDUP_EXACT_EVIDENCE_REFERENCE"):
        if field not in obj:
            return
        before = obj[field]
        after, seen = [], set()
        for item in before:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                after.append(item)
        if len(after) != len(before):
            obj[field] = after
            operations.append(
                {
                    "operation": operation,
                    "path": path + "/" + field,
                    "removed_count": len(before) - len(after),
                }
            )

    # Ownership is NOT a set: detect conflicting assignments before changing any
    # inner evidence list. Equality here includes reasons and every other field.
    for n, group in enumerate(value.get("groups", [])):
        purity = group.get("topic_purity", {})
        roles = purity.get("source_roles", [])
        seen = {}
        for role in roles:
            index = role["candidate_index"]
            if index in seen and seen[index] != role:
                raise NonCanonicalizableProtocolError(
                    f"NON_CANONICALIZABLE_PROTOCOL_ERROR: conflicting source_roles index {index}"
                )
            seen[index] = role
        path = f"/groups/{n}"
        dedup(purity, "source_roles", path + "/topic_purity", "DEDUP_EXACT_SOURCE_ROLE")
        for field in (
            "title_evidence_utterance_ids",
            "importance_evidence_utterance_ids",
        ):
            dedup(group, field, path)
        dedup(purity, "materialization_evidence_utterance_ids", path + "/topic_purity")
        for r, role in enumerate(purity.get("source_roles", [])):
            dedup(
                role, "evidence_utterance_ids", path + f"/topic_purity/source_roles/{r}"
            )
    for n, segment in enumerate(value.get("segments", [])):
        dedup(segment, "title_evidence_indices", f"/segments/{n}")
        for c, claim in enumerate(segment.get("claims", [])):
            dedup(claim, "evidence_indices", f"/segments/{n}/claims/{c}")
    dedup(value, "headline_source_event_ids", "")
    for n, sentence in enumerate(value.get("overview_sentences", [])):
        dedup(sentence, "source_event_ids", f"/overview_sentences/{n}")
    # Explicit outcome citations remain ordered: literal quote validation uses
    # concatenated source text. Never normalize their order or multiplicity.
    return value, (
        {
            "version": CANONICALIZATION_VERSION,
            "original_response": original,
            "canonical_response": copy.deepcopy(value),
            "canonicalization_operations": operations,
        }
        if operations
        else None
    )
