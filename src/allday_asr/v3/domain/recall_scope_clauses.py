"""Two explicitly paired native rooms/dates, not their Cartesian product.
Unrecognised grammar or ambiguous native room identity remains unqueryable.
"""

import re
from copy import deepcopy
from .recall_query import DATE, dates, named_platforms
from .recall_subject_span import address_span
from .recall_investigation_scope import platform_allowed


def paired_plan(question, keyword, people, payload, now, directory):
    if (
        keyword.strip()
        or payload.get("person_id")
        or re.search(r"\d{1,2}:\d{2}|凌晨|早上|上午|中午|下午|晚上|\d{1,2}点", question)
    ):
        return None
    matches = list(re.finditer(DATE, question))
    if len(matches) != 2:
        return None
    prefix = question[: matches[0].start()].strip(" ，,；;")
    actor_platforms = named_platforms(prefix)
    if len(actor_platforms) > 1:
        return None
    prefix = re.sub(r"QQ|微信|录音", "", prefix, flags=re.I).strip()
    actor = ""
    if prefix not in {"", "在", "比较", "对比", "请比较", "请对比"}:
        m = re.fullmatch(r"(?:请问)?(.{1,100}?)在", prefix)
        if not m:
            return None
        actor = address_span(m.group(1), people)
        if not actor:
            return None
    windows = []
    rooms = []
    inherited_year = re.match(r"(\d{4})[-/年]", matches[0].group())
    for i, m in enumerate(matches):
        fragment = question[
            m.end() : matches[i + 1].start() if i == 0 else len(question)
        ]
        labels = []
        for c in directory:
            if c.get("conversation_type") != "group" or not platform_allowed(
                payload, c["platform"]
            ):
                continue
            for n in c["names"]:
                if len(n) > 1 and n in fragment:
                    labels.append((len(n), c, n))
        if not labels:
            return None
        longest = max(x[0] for x in labels)
        labels = [x for x in labels if x[0] == longest]
        choices = {x[1]["key"]: x[1] for x in labels}
        if len(choices) != 1:
            return None
        c = next(iter(choices.values()))
        if actor_platforms and c["platform"] not in actor_platforms:
            return None
        name = next(x[2] for x in labels if x[1]["key"] == c["key"])
        before, after = fragment.split(name, 1)
        # The date must directly modify this room, never another free-standing date.
        before = re.sub(r"QQ|微信", "", before, flags=re.I).strip(' 的在“”「」"‘’')
        if before:
            return None
        platforms = named_platforms(fragment)
        if len(platforms) == 1 and c["platform"] != platforms[0]:
            return None
        label = m.group()
        if i and inherited_year and not re.match(r"\d{4}[-/年]", label):
            label = inherited_year.group(1) + "年" + label
        period, _ = dates(label, payload.get("timezone", "Asia/Singapore"), now)
        first = max(period[0], payload.get("sent_from", period[0]))
        last = min(period[1], payload.get("sent_to", now), now)
        if first > last:
            return None
        windows.append(
            {
                **{
                    k: c[k]
                    for k in (
                        "platform",
                        "source_account_id",
                        "conversation_id",
                        "conversation_type",
                    )
                },
                "sent_from": first,
                "sent_to": last,
            }
        )
        rooms.append(deepcopy(c))
    first = min(w["sent_from"] for w in windows)
    last = max(w["sent_to"] for w in windows)
    if last - first > 366 * 86400:
        return None
    return dict(
        intent="conversation",
        terms=[],
        remote_terms=[],
        exact=False,
        scope=dict(
            person_id="",
            source=payload.get("source", "all"),
            timezone=payload.get("timezone", "Asia/Singapore"),
            sent_from=first,
            sent_to=last,
            relationship="speaker" if actor else "group",
            source_contacts=rooms,
            source_windows=windows,
        ),
        person_mention=actor,
        room_mention="",
        time_explicit=True,
        issue="待核对指定群中的来源发送者；不按同名合并。" if actor else "",
        person_candidates=[],
        contact_candidates=[],
        agentic=True,
        _directory=directory,
        _paired_windows=True,
        _contact_scope_is_seed=False,
    )
