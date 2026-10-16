"""Resolve native targets and optional semantic plans before any message read."""

import json
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from allday_asr.v3.domain.recall_query import understand, dates

GENERIC = {"谁", "别人", "其他人", "大家", "有人"}


def apply_contact(plan, contact, *, seed=False):
    plan["_contact_scope_is_seed"] = seed
    scope = plan["scope"]
    scope["person_id"] = ""
    scope["source"] = contact["platform"]
    scope["source_contacts"] = [deepcopy(contact)]
    plan["issue"] = ""
    plan["contact_candidates"] = []
    scope["target_label"] = contact["display_name"]
    if contact["conversation_type"] == "group" and not contact.get(
        "query_local_address"
    ):
        scope["relationship"] = "group"


def interpret(
    service,
    question,
    keyword,
    people,
    payload,
    now,
    cancel,
    deadline,
    selected=None,
    previous=None,
    owner=None,
):
    directory = []
    notes = []
    requests = 0
    if (
        service.directory
        and payload.get("source") != "recording"
        and keyword != question
    ):
        directory, notes, requests = service.directory.load(
            payload.get("source", "all"),
            payload.get("expand", True),
            cancel,
            min(deadline, time.monotonic() + 12),
        )
    directory = [
        r for r in directory if payload.get("source", "all") in {"all", r["platform"]}
    ]
    book = deepcopy(people)
    for contact in directory:
        key = "native:" + contact["key"]
        book[key] = {
            "person_id": key,
            "display_name": contact["display_name"],
            "aliases": contact["names"],
            "kind": "contact",
        }
    error = ""
    try:
        fast = understand(question, keyword, book, payload, now)
    except ValueError as exc:
        error = str(exc)
        fast = {
            "intent": "conversation",
            "terms": [],
            "remote_terms": [],
            "exact": False,
            "scope": {
                "person_id": payload.get("person_id", ""),
                "source": payload.get("source", "all"),
                "timezone": payload.get("timezone", "Asia/Singapore"),
                "sent_from": payload.get("sent_from", now - 366 * 86400),
                "sent_to": payload.get("sent_to", now),
                "relationship": "speaker",
            },
            "person_mention": "",
            "time_explicit": False,
            "issue": error,
            "person_candidates": [],
        }
    if (
        fast["scope"]["source"] in {"qq", "wechat", "recording"}
        and payload.get("source", "all") == "all"
    ):
        directory = [r for r in directory if r["platform"] == fast["scope"]["source"]]
        filtered = deepcopy(people)
        for contact in directory:
            key = "native:" + contact["key"]
            filtered[key] = {
                "person_id": key,
                "display_name": contact["display_name"],
                "aliases": contact["names"],
                "kind": "contact",
            }
        if not error:
            fast = understand(question, keyword, filtered, payload, now)
    scope = fast["scope"]
    fast["contact_lookup_allowed"] = (
        not error and not payload.get("person_id") and scope["source"] != "recording"
    )
    native = {r["key"]: r for r in directory}
    # Prefer a source-native exact match over a same-label unified card. Keep
    # distinct namespaces as candidates rather than merging them by name.
    mention = fast["person_mention"]
    exact = (
        [
            r
            for r in directory
            if mention.casefold() in [n.casefold() for n in r["names"]]
        ]
        if mention
        else []
    )
    fast["contact_candidates"] = []
    fast["directory_notes"] = notes
    fast["directory_requests"] = requests
    fast["understanding_calls"] = 0
    if selected:
        apply_contact(fast, selected)
        return fast
    if (
        service.preferences
        and owner
        and mention
        and not error
        and not payload.get("person_id")
    ):
        remembered = service.preferences.resolve(
            owner, mention, scope["source"], directory
        )
        if remembered:
            contact, preference = remembered
            apply_contact(fast, contact, seed=True)
            fast["target_preference"] = deepcopy(preference)
            return fast
    unified = [
        pid
        for pid, p in people.items()
        if p["kind"] != "self"
        and mention.casefold()
        in [str(n).casefold() for n in [p["display_name"], *p.get("aliases", [])]]
    ]
    linked = (
        service.directory.linked_many(directory, unified[0])
        if len(unified) == 1 and service.directory
        else []
    )
    if (
        exact
        and len(unified) == 1
        and scope["source"] == "all"
        and all(c["key"] in {r["key"] for r in linked} for c in exact)
    ):
        scope["person_id"] = unified[0]
        scope["linked_contacts"] = linked
        scope["target_label"] = people[unified[0]]["display_name"]
        fast["issue"] = ""
        exact = []
    if scope["person_id"] in people:
        scope["target_label"] = people[scope["person_id"]]["display_name"]
        if service.directory:
            scope["linked_contacts"] = service.directory.linked_many(
                directory, scope["person_id"]
            )
    if exact and not payload.get("person_id"):
        if len(exact) == 1 and all(
            r.get("directory_complete", True) for r in directory
        ):
            apply_contact(fast, exact[0], seed=True)
        else:
            fast["contact_candidates"] = exact[:20]
            fast["scope"]["person_id"] = ""
            fast["issue"] = f"你说的{mention}是？请选择联系人，本次选择只用于这次查询。"
            if len(exact) == 1:
                fast["issue"] = (
                    f"已找到“{mention}”的来源联系人，但目录尚未完整覆盖。请确认本次查询对象。"
                )
    elif scope["person_id"].startswith("native:"):
        apply_contact(fast, native[scope["person_id"][7:]], seed=True)
    if not fast["time_explicit"] and "sent_from" not in payload:
        # Unspecified historical time is a visible bounded year, never a hidden
        # thirty-day substitute for an unparsed time expression.
        period, _ = dates("今天", scope["timezone"], now)
        zone = ZoneInfo(scope["timezone"])
        first = datetime.fromtimestamp(now, zone).date() - timedelta(days=365)
        scope["sent_from"] = int(
            datetime.combine(first, datetime.min.time(), zone).timestamp()
        )
        scope["sent_to"] = now
        fast["directory_notes"].append(
            "未指定日期，本次有界查询近一年；更早历史尚未覆盖。"
        )
    if fast["contact_candidates"]:
        return fast
    simple = not error and (
        keyword.strip()
        or (
            fast["intent"] == "conversation"
            and not fast["terms"]
            and fast["person_mention"]
            and (
                not fast["issue"]
                or not any(
                    n.casefold() in question.casefold()
                    for r in directory
                    for n in r["names"]
                )
            )
        )
    )
    simple = simple or (
        not error
        and not fast["issue"]
        and not fast["terms"]
        and fast["intent"] in {"quote", "conversation"}
        and bool(scope.get("person_id") or scope.get("source_contacts"))
    )
    if (
        service.investigator is not None
        and payload.get("summarize", True)
        and not keyword.strip()
        and not selected
    ):
        # Preserve hard conflicts; only unsupported natural dates may be resolved semantically.
        recoverable_time = error.startswith("时间描述尚不支持") or error.startswith(
            "请使用明确日期或日期范围描述时间"
        )
        if error and not recoverable_time:
            return fast
        if recoverable_time:
            # Date parsing happens before platform extraction in the fast path.
            # Recovering time must not lose an explicit platform from the question.
            named_sources = [
                p
                for p, label in (
                    ("qq", "QQ"),
                    ("wechat", "微信"),
                    ("recording", "录音"),
                )
                if re.search(label, question, re.I)
            ]
            if len(named_sources) > 1 or (
                named_sources and scope["source"] not in {"all", named_sources[0]}
            ):
                fast["issue"] = "问题来源与筛选范围不一致，请明确来源。"
                return fast
            if named_sources:
                scope["source"] = named_sources[0]
                directory = [r for r in directory if r["platform"] == scope["source"]]
        if not (payload.get("person_id") and fast["issue"]) and (
            not simple or fast["issue"]
        ):
            fast.update(agentic=True, _directory=directory, _parse_error=bool(error))
            return fast
    if simple or service.planner is None:
        if (
            fast["issue"]
            and mention
            and not scope.get("source_contacts")
            and not scope["person_id"]
            and not fast["person_candidates"]
        ):
            fast["issue"] = (
                f"当前来源联系人目录未能确定“{mention}”。请补充来源或原生备注名称；无需先建立人物卡。"
            )
        if error and service.planner is None:
            raise ValueError(error)
        return fast
    candidates = []
    prior_keys = (
        {c["key"] for c in previous["scope"].get("source_contacts", [])}
        if previous
        else set()
    )
    prioritized = sorted(
        directory,
        key=lambda r: (
            not (
                r["key"] in prior_keys
                or any(n.casefold() in question.casefold() for n in r["names"])
            )
        ),
    )
    for r in prioritized:
        candidate = {k: r[k] for k in ["key", "platform", "display_name", "names"]}
        if (
            len(json.dumps(candidates + [candidate], ensure_ascii=False).encode())
            > 14000
        ):
            break
        candidates.append(candidate)
    context = None
    if previous:
        prior = previous["scope"]
        context = {
            "source": prior["source"],
            "contact_keys": [c["key"] for c in prior.get("source_contacts", [])],
            "person_id": prior.get("person_id", ""),
            "terms": previous["terms"],
            "intent": previous["intent"],
            "claims": [
                {"kind": c["kind"], "text": c["text"][:700]}
                for c in previous.get("claims", [])[:3]
            ],
        }
    before_model = deepcopy(fast)
    try:
        fast["understanding_calls"] = 1
        value = service.planner(
            {
                "query_id": payload["query_id"],
                "question": question,
                "local_now": datetime.fromtimestamp(
                    now, ZoneInfo(scope["timezone"])
                ).isoformat(),
                "contacts": candidates,
                "previous_query": context,
                "hard_scope": {
                    "source": payload.get("source", "all"),
                    "person_id": payload.get("person_id", ""),
                    "sent_from": payload.get("sent_from"),
                    "sent_to": payload.get("sent_to"),
                },
            },
            cancel,
        )
        if cancel.is_set() or time.monotonic() > deadline:
            raise TimeoutError()
        if (
            not isinstance(value, dict)
            or value.get("intent")
            not in {"quote", "reason", "timeline", "latest", "conversation"}
            or value.get("relationship")
            not in {"speaker", "conversation", "group", "topic"}
        ):
            raise ValueError("invalid semantic plan")
        topics = value.get("topics")
        keys = value.get("contact_keys")
        name = value.get("person_mention")
        if (
            not isinstance(topics, list)
            or len(topics) > 8
            or any(not isinstance(t, str) or not 1 <= len(t) <= 100 for t in topics)
        ):
            raise ValueError("invalid topics")
        if (
            not isinstance(keys, list)
            or len(keys) > 20
            or any(k not in {r["key"] for r in candidates} for k in keys)
            or not isinstance(name, str)
        ):
            raise ValueError("invented contact")
        if (
            mention
            and mention not in GENERIC
            and not name
            and not scope.get("person_id")
            and not scope.get("source_contacts")
        ):
            raise ValueError("dropped person constraint")
        if value.get("clarification"):
            fast["issue"] = str(value["clarification"])[:500]
            return fast
        period = value.get("time", {})
        if not isinstance(period, dict):
            raise ValueError("invalid time")
        if period.get("kind") == "unresolved":
            fast["issue"] = "时间范围未能确定，请补充日期；本轮未检索。"
            return fast
        if not fast["time_explicit"] and period.get("kind") != "unchanged":
            zone = ZoneInfo(scope["timezone"])
            today = datetime.fromtimestamp(now, zone).date()
            if period.get("kind") == "offset_days":
                a, b = int(period["first"]), int(period["last"])
                if not -3660 <= a <= b <= 0 or re.search(r"点|:\d{2}", question):
                    raise ValueError("unresolved clock constraint")
                first, last = today + timedelta(days=a), today + timedelta(days=b)
            elif period.get("kind") == "date_range":
                first = datetime.strptime(period["first"], "%Y-%m-%d").date()
                last = datetime.strptime(period["last"], "%Y-%m-%d").date()
            else:
                raise ValueError("invalid time kind")
            a = int(datetime.combine(first, datetime.min.time(), zone).timestamp())
            b = (
                int(
                    datetime.combine(
                        last + timedelta(days=1), datetime.min.time(), zone
                    ).timestamp()
                )
                - 1
            )
            scope["sent_from"] = max(a, payload.get("sent_from", a))
            scope["sent_to"] = min(b, payload.get("sent_to", now), now)
            if not 0 <= scope["sent_from"] <= scope["sent_to"] or b - a > 366 * 86400:
                raise ValueError("invalid range")
            fast["time_explicit"] = True
        elif error:
            raise ValueError("unresolved time cannot default")
        fast["contact_lookup_allowed"] = (
            not payload.get("person_id") and scope["source"] != "recording"
        )
        fast.update(
            intent=value["intent"],
            terms=[keyword.strip()] if keyword.strip() else topics,
            remote_terms=[keyword.strip()] if keyword.strip() else topics[:2],
            person_mention=name,
        )
        scope["relationship"] = value["relationship"]
        if keys and not scope.get("person_id") and not scope.get("source_contacts"):
            proposed = [native[k] for k in dict.fromkeys(keys)]
            named_in_question = [
                r
                for r in directory
                if any(n.casefold() in question.casefold() for n in r["names"])
            ]
            grounded_keys = {r["key"] for r in named_in_question}
            if any(r["key"] not in grounded_keys for r in proposed):
                raise ValueError("contact proposal lacks a real name/alias clue")
            exact_model = [
                r
                for r in proposed
                if name.casefold() in [n.casefold() for n in r["names"]]
                and len(named_in_question) == 1
                and named_in_question[0]["key"] == r["key"]
            ]
            if (
                len(proposed) == 1
                and len(exact_model) == 1
                and all(r.get("directory_complete", True) for r in directory)
            ):
                apply_contact(fast, proposed[0])
            else:
                fast["contact_candidates"] = proposed
                fast["issue"] = (
                    f"你说的{name or mention}是？请选择联系人，本次选择只用于这次查询。"
                )
        elif name and not scope.get("person_id") and not scope.get("source_contacts"):
            fast["issue"] = f"来源目录中没有足够依据确定“{name}”；请补充来源或称呼。"
        elif (
            not name
            and not keys
            and previous
            and not mention
            and value["relationship"] != "topic"
        ):
            prior = previous["scope"]
            if prior.get("source_contacts") and any(
                c["key"] not in native for c in prior["source_contacts"]
            ):
                raise ValueError("prior contact unavailable")
            for field in [
                "source_contacts",
                "source_accounts",
                "linked_contacts",
                "person_id",
                "source",
                "relationship",
                "target_label",
            ]:
                if field in prior:
                    scope[field] = deepcopy(prior[field])
            if payload.get("source", "all") not in {"all", scope["source"]}:
                raise ValueError("context source conflict")
            if not fast["time_explicit"]:
                scope["sent_from"] = max(prior["sent_from"], now - 365 * 86400)
                scope["sent_to"] = min(payload.get("sent_to", now), now)
            if not fast["terms"]:
                fast["terms"] = previous["terms"]
                fast["remote_terms"] = previous["terms"][:2]
            fast["issue"] = ""
        elif not name and fast["terms"]:
            if mention and mention not in GENERIC:
                raise ValueError("dropped person constraint")
            fast["issue"] = ""
            scope["source_accounts"] = list(
                {
                    (r["platform"], r["source_account_id"]): {
                        "platform": r["platform"],
                        "source_account_id": r["source_account_id"],
                    }
                    for r in directory
                }.values()
            )[:20]
        elif not scope.get("person_id") and not scope.get("source_contacts"):
            fast["issue"] = "请补充聊天主题或交流对象，当前问题还不能确定检索范围。"
        if (
            not fast["terms"]
            and not scope.get("person_id")
            and not scope.get("source_contacts")
        ):
            fast["issue"] = fast["issue"] or "请明确交流对象或主题。"
    except Exception:
        fast = before_model
        fast["understanding_calls"] = 1
        fast["issue"] = (
            "无法可靠理解本次问题；请补充人物称呼、主题或日期后重试。本轮没有扩大范围。"
        )
    return fast


def choose(service, owner, payload):
    if (
        set(payload) - {"action", "query_id", "contact_key", "remember"}
        or type(payload.get("remember", False)) is not bool
    ):
        raise ValueError("联系人选择字段无效")
    saved = service._saved(owner, payload.get("query_id"))
    candidate = next(
        (
            c
            for c in [
                *saved.get("contact_candidates", []),
                *saved.get("_lookup_candidates", []),
            ]
            if c["key"] == payload.get("contact_key")
        ),
        None,
    )
    if candidate is None:
        raise ValueError("联系人选择已过期，请重新查询")
    if not saved.get("contact_lookup_allowed"):
        raise ValueError("请先明确日期或已有的人物筛选；联系人选择不会改变这些约束")
    prepared = deepcopy(saved["_plan"])
    if saved.get("investigation_version"):
        # A clarification is a continuation of the same query, not a budget reset.
        prepared["_carried_metrics"] = deepcopy(saved["metrics"])
        prepared["_remaining_seconds"] = max(
            0, 60 - saved["metrics"]["elapsed_ms"] / 1000
        )
    mention = saved["_plan"]["person_mention"]
    if payload.get("remember") and (not service.preferences or not mention):
        raise ValueError("当前问题没有可记忆的明确称呼；可仅本次选择")
    result = service.query(
        owner, saved["_request"], selected=candidate, prepared=prepared
    )
    if payload.get("remember") and result.get("status") != "cancelled":
        preference = service.preferences.remember(owner, mention, candidate)
        current = service._saved(owner, saved["query_id"])
        current["target_preference"] = deepcopy(preference)
        current["_plan"]["target_preference"] = deepcopy(preference)
        result["target_preference"] = deepcopy(preference)
    return result
