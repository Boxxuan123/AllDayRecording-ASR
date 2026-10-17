"""Literal user-provided event anchors; no topic/person dictionaries."""

import re

IDENTIFIER = r"(?<![A-Za-z0-9_-])[A-Za-z]{2,12}-[0-9]{2,12}(?![A-Za-z0-9_-])"


def event_anchors(question, person_mention=""):
    quoted = re.findall(r'[“「"‘]([^”」"’\n]{2,64})[”」"’]', question)
    identifiers = re.findall(IDENTIFIER, question)
    return list(dict.fromkeys(s for s in quoted + identifiers if s != person_mention))[
        :2
    ]


def literal_anchor(text, anchors):
    for anchor in anchors:
        if re.fullmatch(IDENTIFIER, anchor):
            if re.search(
                r"(?<![A-Za-z0-9_-])" + re.escape(anchor) + r"(?![A-Za-z0-9_-])",
                text,
                re.I,
            ):
                return anchor
        elif anchor.casefold() in text.casefold():
            return anchor
    return None


def conflicting_identifier(text, anchors):
    identifiers = re.findall(IDENTIFIER, text)
    expected = [a.casefold() for a in anchors if re.fullmatch(IDENTIFIER, a)]
    return bool(
        expected
        and identifiers
        and not any(i.casefold() in expected for i in identifiers)
    )
