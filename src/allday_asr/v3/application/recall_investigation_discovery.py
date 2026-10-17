"""Bounded discovery of native senders and quoted topics, without identity writes."""

from copy import deepcopy
import unicodedata

from allday_asr.v3.domain.recall_investigation_scope import (
    remote_grants,
    contact_record_allowed,
    inside_root,
)
from allday_asr.v3.ports.chat_data import ChatDataError


def _name(value):
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


def seed_quoted_topic(tools, action):
    # A quoted topic is a retrieval lead, not proof of the named person's identity.
    if not tools.anchors or tools.plan.get("exact"):
        return False
    hits = tools.run(action("search_messages", terms=tools.anchors[:1]), [])
    if not hits:
        return False
    targets = []
    rooms = set()
    for ref in hits:
        row = tools.refs[ref]
        key = (row["source"], row["source_account_id"], row["conversation_id"])
        if key not in rooms:
            targets.append(ref)
            rooms.add(key)
        if len(targets) == 2:
            break
    if targets and tools.remaining()["source_requests"] > 2:
        tools.run(action("get_message_context", targets=targets), [])
    return True


def seed_observed_sender(tools, action):
    """Read a small authorised episode to distinguish sender metadata from body words."""
    mention = tools.plan.get("person_mention")
    if not mention or tools.plan.get("exact") or tools.plan.get("_source_candidates"):
        return False
    if tools.result["scope"].get("person_id") or tools.result["scope"].get(
        "source_contacts"
    ):
        return False
    scope = tools.scope("target")
    if scope is None:
        return False
    scope = deepcopy(scope)
    scope["relationship"] = "topic"
    grants = remote_grants(
        scope,
        tools.service.sources.authorized_scopes(scope),
        tools.authorized_directory,
    )
    if not grants or not tools.result["_request"].get("expand", True):
        return False
    # Question-named room metadata can prioritise a namespace, never establish a person merge.
    from difflib import SequenceMatcher

    question = tools.result["_request"]["question"].replace(mention, "")

    def score(grant):
        names = [
            c["display_name"]
            for c in tools.directory
            if c["platform"] == grant["platform"]
            and c["source_account_id"] == grant["source_account_id"]
            and c["conversation_type"] == "group"
        ]
        return max(
            (
                sum(
                    b.size**2
                    for b in SequenceMatcher(
                        None, n, question, autojunk=False
                    ).get_matching_blocks()
                    if b.size >= 2
                )
                for n in names
            ),
            default=0,
        )

    grants.sort(key=lambda g: (-score(g), g["platform"], g["source_account_id"]))
    # Prefer question-related native rooms before an account-wide sender probe.
    # Greedy coverage gives two separately named rooms a chance; activity counts
    # and directory order cannot push a quiet room out of the candidate list.
    groups = []
    for c in tools.directory:
        if c["conversation_type"] != "group" or not inside_root(c, tools.root_scope):
            continue
        if not any(
            all(
                not g.get(k) or g[k] == c.get(k)
                for k in ("platform", "source_account_id", "conversation_id")
            )
            for g in grants
        ):
            continue
        blocks = [
            b
            for b in SequenceMatcher(
                None, c["display_name"], question, autojunk=False
            ).get_matching_blocks()
            if b.size >= 2
        ]
        if blocks:
            groups.append((c, {i for b in blocks for i in range(b.b, b.b + b.size)}))
    covered = set()
    selected = []
    for _ in range(2):
        if not groups:
            break
        c, positions = min(
            groups,
            key=lambda item: (
                -len(item[1] - covered),
                -len(item[1]),
                item[0]["platform"],
                item[0]["source_account_id"],
                item[0]["conversation_id"],
            ),
        )
        selected.append(
            {k: c[k] for k in ("platform", "source_account_id", "conversation_id")}
        )
        covered.update(positions)
        groups = [item for item in groups if item[0]["key"] != c["key"]]
    probes = selected or grants[:2]
    tools.verify_remote()
    found = []
    for grant in probes:
        left = tools.remaining()
        if left["source_requests"] <= 3 or left["body_requests"] <= 2:
            break
        limit = min(40 if selected else 60, left["remote_records"] - 50)
        if limit <= 0:
            break
        remote = {**grant, "sent_from": scope["sent_from"], "sent_to": scope["sent_to"]}
        part = tools.remote("/v1/messages", scope=remote, limit=limit)
        records = part.get("items")
        if not isinstance(records, list) or len(records) > limit:
            raise ChatDataError("INVALID_CHAT_PAGE", 502)
        tools.result["metrics"]["source_records_read"] += len(records)
        rows = []
        for record in records:
            tools.validate_native_record(record)
            if (
                any(record.get(k) != v for k, v in grant.items())
                or not scope["sent_from"]
                <= (record.get("sent_at") or -1)
                <= scope["sent_to"]
                or not contact_record_allowed(record, scope)
            ):
                raise ChatDataError("CONTEXT_IDENTITY_MISMATCH", 502)
            if (
                _name(record.get("sender_display_name")) == _name(mention)
                and record.get("sender_account_id")
                and record.get("is_from_self") is False
            ):
                row = tools.service.sources.evidence(record, "mac")
                if inside_root(row, tools.root_scope, evidence=True):
                    rows.append(
                        {
                            **row,
                            "retrieval_basis": "native_sender_name_candidate",
                            "relevance_confirmed": False,
                        }
                    )
        tools.current_scope = scope
        refs = tools.add(rows)
        found.extend(refs)
        tools.result.setdefault("sender_discovery", []).append(
            {
                "namespace": grant,
                "records_read": len(records),
                "matched_record_refs": refs,
                "has_more": bool(part.get("has_more")),
                "persistent_person_merge": False,
            }
        )
    if not found:
        return False
    actors = {}
    for ref in found:
        row = tools.refs[ref]
        key = (row["source"], row["source_account_id"], row["sender_account_id"])
        actors.setdefault(key, []).append(ref)
    tools.result["sender_candidate_relations"] = [
        dict(
            platform=k[0],
            source_account_id=k[1],
            sender_account_id=k[2],
            evidence_ids=v,
            evidence_versions={ref: tools.refs[ref]["revision"] for ref in v},
            basis="observed_native_name_candidate",
            persistent_person_merge=False,
        )
        for k, v in actors.items()
    ]
    targets = []
    rooms = set()
    for ref in found:
        row = tools.refs[ref]
        key = (row["source"], row["source_account_id"], row["conversation_id"])
        if key not in rooms:
            targets.append(ref)
            rooms.add(key)
        if len(targets) == 2:
            break
    if targets and tools.remaining()["source_requests"] > 2:
        tools.run(action("get_message_context", targets=targets), [])
    return True


def resolve_sender_subject(tools):
    """Resolve a scoped speaker-label recap, without confirming a real-world person."""
    if (tools.plan.get("intent") != "conversation"
            or tools.plan["scope"].get("relationship") != "speaker"
            or tools.plan.get("person_candidates")):
        return False
    candidates = tools.result.get("sender_candidate_relations", [])
    if len(candidates) != 1:
        return False
    candidate = candidates[0]
    refs = candidate["evidence_ids"]
    rows = [tools.refs[r] for r in refs if r in tools.refs]
    native_matches = {ref for d in tools.result.get("sender_discovery", [])
                      for ref in d["matched_record_refs"]}
    keys = ("source", "source_account_id", "sender_account_id")
    actor = (candidate["platform"], candidate["source_account_id"],
             candidate["sender_account_id"])
    if (not rows or len(rows) != len(refs)
            or candidate.get("basis") != "observed_native_name_candidate"
            or not set(refs) <= native_matches
            or tools.service.sources.changed(rows)
            or any(tuple(r.get(k) for k in keys) != actor
                   or r.get("revision") != candidate.get("evidence_versions", {}).get(r["id"])
                   or r.get("source_is_from_self") is not False
                   or not inside_root(r, tools.root_scope, evidence=True)
                   for r in rows)):
        return False
    tools.result["subject_resolution"] = dict(
        basis="single_observed_native_sender", evidence_ids=list(refs),
        person_identity_confirmed=False, persistent_person_merge=False)
    tools.result.update(contact_lookup_allowed=False, contact_candidates=[])
    tools.note("本次按来源昵称与同一原生账号归纳已取回的发言，未确认或合并现实人物身份。")
    return True
