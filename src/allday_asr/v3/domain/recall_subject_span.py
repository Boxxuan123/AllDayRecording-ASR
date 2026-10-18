"""Grammatical address/room spans; never creates identity or source relations."""

import re


def known_label(value, people):
    return any(
        value.casefold() == n.strip().casefold()
        for p in people.values()
        for n in [p["display_name"], *p.get("aliases", [])]
        if isinstance(n, str) and n.strip()
    )


def quoted_label(value):
    for left, right in [("“", "”"), ("「", "」"), ('"', '"'), ("‘", "’"), ("'", "'")]:
        m = re.fullmatch(
            re.escape(left) + "([^" + re.escape(right) + "]+)" + re.escape(right), value
        )
        if m:
            return m[1]
    return None


def address_span(value, people):
    value = value.strip(" ，。？?")
    if known_label(value, people):
        return value
    if value.endswith("的") and known_label(value[:-1], people):
        return value[:-1]
    raw = re.sub(r"^(?:备注名|备注|昵称)(?:为|是)?\s*", "", value)
    if raw.endswith("的"):
        raw = raw[:-1].rstrip()
    label = quoted_label(raw)
    return label if label is not None else value


def address_context(value, people):
    """Separate a native subject from a grammatical conversation modifier.

    A literal full native label wins. Unknown prefixes remain unresolved; this
    extracts grammar and never establishes an alias or an account relation.
    """
    normalized = address_span(value, people)
    if known_label(normalized, people):
        return normalized, ""
    match = re.fullmatch(
        r"(.+?)\s*在\s*(?:(?:微信|QQ|录音)\s*)?(?:里|中|上)?\s*"
        r"((?:这|那)(?:段|次).{1,100})的",
        value.strip(),
        re.I,
    )
    if match:
        name = address_span(match[1], people)
        if known_label(name, people):
            return name, match[2]
    return normalized, ""


def group_topic_span(value, people):
    # Full observed labels may themselves contain grammatical words.
    if known_label(value.strip(), people):
        return None
    parts = re.split(r"关于|有关|围绕", value, maxsplit=1)
    if len(parts) != 2:
        return None
    head = re.sub(r"^在\s*", "", parts[0].strip())
    # Quotes around the entire native group name are preserved internally.
    literal = quoted_label(head)
    if literal is not None:
        return (literal, parts[1]) if re.search(r"群(?:聊)?$", literal) else None
    m = re.fullmatch(r"(.+?)(?:群聊|群)(?:里|中|内)?", head)
    if not m:
        return None
    quoted = quoted_label(m[1])
    if quoted is not None:
        return quoted, parts[1]
    # Without quotes, the written group suffix can be part of its native name.
    label = re.sub(r"(?:里|中|内)$", "", head)
    return label, parts[1]
