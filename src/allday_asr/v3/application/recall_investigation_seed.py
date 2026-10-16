"""Small on-demand topic/actor exploration, using only existing read capabilities."""

import re


def seed_topic(tools, action):
    terms = tools.plan.get("terms", [])
    if tools.plan.get("person_mention") or not terms:
        return False

    def match(contact):
        return contact["conversation_type"] == "group" and any(
            re.search(r"(?<![a-z0-9_])" + re.escape(t) + r"(?![a-z0-9_])", n, re.I)
            for t in terms
            for n in contact["names"]
        )

    groups = [c for c in tools.directory if match(c)]
    if not groups:
        return False
    # Exact named-entity boundaries before embedded abbreviations or year-only labels.
    # Keep source directory order as the stable tiebreaker, never a supplied answer ID.
    groups.sort(
        key=lambda c: (
            not any(
                re.match(re.escape(t) + r"[^a-zA-Z0-9_]", n, re.I)
                for t in terms
                for n in c["names"]
            )
        )
    )
    groups = groups[:3]
    tools.result["candidate_schedule"] = [
        {
            "key": c["key"],
            "name": c["display_name"],
            "basis": "native_group_name_entity_boundary",
            "physical_body_requests": 1,
        }
        for c in groups
    ]
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
        if row.get("source_is_from_self") is not False or not row.get(
            "sender_account_id"
        ):
            continue
        key = (row["source"], row["source_account_id"], row["sender_account_id"])
        actors.setdefault(key, []).append(row)
    import hashlib

    actor_refs = []
    for key, rows in sorted(
        actors.items(),
        key=lambda pair: (-len({r["conversation_id"] for r in pair[1]}), -len(pair[1])),
    )[:6]:
        value = dict(zip(("platform", "source_account_id", "sender_account_id"), key, strict=True))
        value.update(
            display_name=rows[-1]["speaker"],
            basis="observed_sender",
            observed_conversations=len({r["conversation_id"] for r in rows}),
            observed_messages=len(rows),
            citations=[r["id"] for r in rows[:2]],
            person_id="",
        )
        actor_refs.append(
            tools.ref(
                "account", value, hashlib.sha256(repr(key).encode()).hexdigest()[:16]
            )
        )
    for ref in actor_refs[:2]:
        if tools.remaining()["source_requests"] > 3:
            tools.run(
                action("get_related_conversations", targets=[ref], window="episode"), []
            )
    return True


def seed_person(tools, action):
    """Explore independent current-episode candidates before historical discovery."""
    contacts = tools.plan.get("_source_candidates") or tools.result["scope"].get(
        "source_contacts", []
    )
    if (
        not tools.plan.get("person_mention")
        or not tools.plan.get("_contact_scope_is_seed")
        or not contacts
    ):
        return False
    if re.search(
        r"称呼|外号|对应.{0,8}账号|查清", tools.result["_request"]["question"]
    ):
        return False
    # An exact native label in one namespace must not suppress an independently
    # grounded alias in another. Names are identity-discovery terms only here.
    covered = {c["platform"] for c in contacts}
    missing = {c["platform"] for c in tools.directory} - covered
    if (
        missing
        and tools.remaining()["source_requests"] > 8
        and tools.remaining()["body_requests"] > 5
    ):
        hits = tools.run(
            action(
                "search_messages",
                terms=[tools.plan["person_mention"]],
                window="identity",
            ),
            [],
        )
        direct = [
            h
            for h in hits
            if tools.refs[h].get("conversation_type") == "c2c"
            and tools.refs[h].get("source_is_from_self") is True
            and tools.refs[h].get("source") in missing
        ]
        if direct:
            tools.run(
                action("get_message_context", targets=direct[:1], window="identity"), []
            )
            from allday_asr.v3.domain.recall_address import direct_address

            resolved = direct_address(
                [r for r in tools.result["evidence"] if r["source"] in missing],
                tools.plan["person_mention"],
                tools.directory,
            )
            if resolved:
                c, basis = resolved
                contacts = [*contacts[:3], c]
                tools.result.setdefault("source_candidate_relations", []).append(
                    dict(
                        contact_key=c["key"],
                        basis="direct_address_with_peer_reply",
                        evidence_ids=[r["id"] for r in basis],
                        persistent_person_merge=False,
                    )
                )
    actors = []
    rooms = set()
    episode_hits = []
    unknown_private = []
    for c in contacts[:4]:
        if tools.remaining()["source_requests"] <= 3:
            break
        ref = tools.ref("contact", c, c["key"])
        rooms.add((c["platform"], c["source_account_id"], c["conversation_id"]))
        hits = tools.run(
            action("search_messages", targets=[ref], window="message_day"), []
        )
        episode_hits.extend(hits)
        peers = [
            h
            for h in hits
            if tools.refs[h].get("source_is_from_self") is False
            and tools.refs[h].get("sender_account_id")
            and (
                not c.get("sender_account_id")
                or tools.refs[h]["sender_account_id"] == c["sender_account_id"]
            )
        ]
        if c["conversation_type"] == "c2c" and peers:
            actors.append(peers[0])
        elif c["conversation_type"] == "c2c" and c.get("sender_account_id"):
            # A native private peer is an authorized lookup candidate, not a permanent person link.
            import hashlib

            actor = {
                k: c[k] for k in ("platform", "source_account_id", "sender_account_id")
            }
            actor.update(
                display_name=c["display_name"],
                basis="native_private_peer_directory",
                directory_contact_ref=ref,
                citations=[],
                person_id="",
            )
            actors.append(
                tools.ref(
                    "account",
                    actor,
                    hashlib.sha256(repr(actor).encode()).hexdigest()[:16],
                )
            )
        elif c["conversation_type"] == "c2c":
            unknown_private.append(ref)
    # Historical private speech is only needed to discover a missing native peer.
    # Probe each namespace after current candidates, not an entire old platform first.
    for ref in unknown_private[:2]:
        if tools.remaining()["source_requests"] <= 4:
            break
        hits = tools.run(
            action("search_messages", targets=[ref], window="discovery"), []
        )
        peers = [
            h
            for h in hits
            if tools.refs[h].get("source_is_from_self") is False
            and tools.refs[h].get("sender_account_id")
        ]
        if peers:
            actors.append(peers[0])
    seen = set()
    actors.sort(key=lambda r: tools.refs[r]["type"] != "evidence")
    for actor in actors:
        value = tools.refs[actor]
        key = (
            value.get("source", value.get("platform")),
            value.get("source_account_id"),
            value.get("sender_account_id"),
        )
        if key in seen or len(seen) >= 2 or tools.remaining()["source_requests"] <= 3:
            continue
        seen.add(key)
        related = tools.run(
            action("get_related_conversations", targets=[actor], window="message_day"),
            [],
        )
        new = [
            r
            for r in related
            if (
                tools.refs[r]["platform"],
                tools.refs[r]["source_account_id"],
                tools.refs[r]["conversation_id"],
            )
            not in rooms
        ]
        support = [actor] if value["type"] == "evidence" else []
        if new and tools.remaining()["source_requests"] > 2:
            tools.run(
                {
                    **action("search_messages", targets=new[:2], window="message_day"),
                    "evidence_ids": support,
                },
                [],
            )
    if not episode_hits and not actors and tools.remaining()["source_requests"] > 4:
        c = contacts[0]
        tools.run(
            action(
                "search_messages", targets=["contact:" + c["key"]], window="discovery"
            ),
            [],
        )
    return True
