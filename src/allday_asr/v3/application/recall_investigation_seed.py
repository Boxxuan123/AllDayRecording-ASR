"""Small on-demand topic/actor exploration, using only existing read capabilities."""
import re


def seed_topic(tools, action):
    terms = tools.plan.get("terms", [])
    if tools.plan.get("person_mention") or not terms:
        return False
    def match(contact):
        return contact["conversation_type"] == "group" and any(
            re.search(r"(?<![a-z0-9_])" + re.escape(t) + r"(?![a-z0-9_])", n, re.I)
            for t in terms for n in contact["names"])
    groups = [c for c in tools.directory if match(c)]
    if not groups:
        return False
    # Exact named-entity boundaries before embedded abbreviations or year-only labels.
    # Keep source directory order as the stable tiebreaker, never a supplied answer ID.
    groups.sort(key=lambda c: not any(
        re.match(re.escape(t) + r"[^a-zA-Z0-9_]", n, re.I)
        for t in terms for n in c["names"]))
    groups = groups[:3]
    tools.result["candidate_schedule"] = [{"key": c["key"], "name": c["display_name"],
        "basis": "native_group_name_entity_boundary", "physical_body_requests": 1} for c in groups]
    for index, contact in enumerate(groups):
        ref = tools.ref("contact", contact, contact["key"])
        if index == 0:
            tools.run(action("search_messages", targets=[ref]), [])
        # Old group speech is an actor lead only, not a dated event conclusion.
        if tools.remaining()["source_requests"] > 5:
            tools.run(action("search_messages", targets=[ref], window="discovery"), [])

    # Provenance-ranked actor leads, never formal membership or cross-platform merging.
    actors = {}
    for row in tools.result["evidence"]:
        if row.get("source_is_from_self") is not False or not row.get("sender_account_id"):
            continue
        key = (row["source"], row["source_account_id"], row["sender_account_id"])
        actors.setdefault(key, []).append(row)
    import hashlib
    actor_refs = []
    for key, rows in sorted(actors.items(), key=lambda pair: (
            -len({r["conversation_id"] for r in pair[1]}), -len(pair[1])))[:6]:
        value = dict(zip(("platform", "source_account_id", "sender_account_id"), key))
        value.update(display_name=rows[-1]["speaker"], basis="observed_sender",
            observed_conversations=len({r["conversation_id"] for r in rows}),
            observed_messages=len(rows), citations=[r["id"] for r in rows[:2]], person_id="")
        actor_refs.append(tools.ref("account", value, hashlib.sha256(repr(key).encode()).hexdigest()[:16]))
    for ref in actor_refs[:2]:
        if tools.remaining()["source_requests"] > 3:
            tools.run(action("get_related_conversations", targets=[ref], window="episode"), [])
    return True



def seed_person(tools, action):
    """A named native private room supplies an actor lead, never a person hard scope."""
    contacts = [c for c in (tools.plan.get("_source_candidates") or tools.result["scope"].get("source_contacts", [])) if c["conversation_type"] == "c2c"]
    if not tools.plan.get("person_mention") or not tools.plan.get("_contact_scope_is_seed") or not contacts:
        return False
    if re.search(r"称呼|外号|对应.{0,8}账号|查清",tools.result["_request"]["question"]):
        return False
    for c in contacts[:2]:
        ref=tools.ref("contact",c,c["key"])
        hits=tools.run(action("search_messages",targets=[ref]),[])
        if not hits and tools.remaining()["source_requests"]>4:
            hits=tools.run(action("search_messages",targets=[ref],window="discovery"),[])
        peers=[h for h in hits if tools.refs[h].get("source_is_from_self") is False and tools.refs[h].get("sender_account_id")]
        if peers and tools.remaining()["source_requests"]>2:
            tools.run(action("get_related_conversations",targets=[peers[0]],window="episode"),[])
    return True
