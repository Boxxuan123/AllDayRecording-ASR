"""Audit geometry only: integer milliseconds, half-open intervals, no gate policy."""

from collections import Counter


def union(ranges):
    result = []
    for lo, hi in sorted(ranges):
        if lo >= hi:
            continue
        if result and lo <= result[-1][1]:
            result[-1][1] = max(hi, result[-1][1])
        else:
            result.append([lo, hi])
    return result


def intersection(a, b):
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return [lo, hi] if lo < hi else None


def position(span, target):
    lo, hi = span
    start, end = target
    if not start <= lo < hi <= end:
        raise ValueError("intersection must be inside target")
    return (
        "FULL"
        if lo == start and hi == end
        else ("START_EDGE" if lo == start else "END_EDGE" if hi == end else "INTERIOR")
    )


def bucket(ms):
    if ms <= 0:
        raise ValueError("positive intersection required")
    for end, label in [
        (25, "0-25"),
        (50, "25-50"),
        (100, "50-100"),
        (200, "100-200"),
        (500, "200-500"),
    ]:
        if ms <= end:
            return label
    return "500+"


def spans(turns, speaker, same=True):
    return [
        (t["start_ms"], t["end_ms"])
        for t in turns
        if (t["speaker_label"] == speaker) == same
    ]


def clipped(ranges, target):
    return union(i for r in ranges if (i := intersection(r, target)))


def analyze(start, end, speaker, regular, exclusive):
    if start >= end:
        raise ValueError("empty target")
    target = [start, end]
    rt = clipped(spans(regular, speaker), target)
    rf = clipped(spans(regular, speaker, False), target)
    et = clipped(spans(exclusive, speaker), target)
    ef = clipped(spans(exclusive, speaker, False), target)
    simultaneous = union(i for a in rt for b in rf if (i := intersection(a, b)))
    components = [
        {
            "start_ms": a,
            "end_ms": b,
            "duration_ms": b - a,
            "position": position([a, b], target),
            "bucket": bucket(b - a),
        }
        for a, b in simultaneous
    ]
    # A fully owned target is a stronger condition than majority assignment.
    full_ownership = et == [target] and not ef
    return {
        "target_range": target,
        "target_label": speaker,
        "target_regular_ranges": rt,
        "target_exclusive_ranges": et,
        "foreign_regular_ranges": rf,
        "foreign_exclusive_ranges": ef,
        "intersections": components,
        "intersection_ms": sum(b - a for a, b in simultaneous),
        "full_target_exclusive_ownership": full_ownership,
        "b_pattern": full_ownership and bool(simultaneous),
        "edge_b_pattern": full_ownership
        and any(c["position"] in ("START_EDGE", "END_EDGE") for c in components),
    }


def pattern_cells(start, end, speaker, regular, exclusive):
    edges = {start, end}
    for t in regular + exclusive:
        if t["start_ms"] < end and t["end_ms"] > start:
            edges.update((max(start, t["start_ms"]), min(end, t["end_ms"])))
    edges = sorted(edges)
    result = Counter()
    for lo, hi in zip(edges, edges[1:], strict=False):
        r = {
            t["speaker_label"]
            for t in regular
            if t["start_ms"] < hi and t["end_ms"] > lo
        }
        e = {
            t["speaker_label"]
            for t in exclusive
            if t["start_ms"] < hi and t["end_ms"] > lo
        }
        if speaker in r and r - {speaker}:
            key = (
                "regular_target+foreign/exclusive_target"
                if e == {speaker}
                else "regular_target+foreign/exclusive_foreign_or_missing"
            )
        elif r == {speaker} and e == {speaker}:
            key = "regular_target_only/exclusive_target"
        else:
            key = "other"
        result[key] += hi - lo
    return dict(result)


def transitions(regular, exclusive):
    result = []
    ordered = sorted(exclusive, key=lambda t: (t["start_ms"], t["end_ms"]))
    for prev, current in zip(ordered, ordered[1:], strict=False):
        if (
            prev["speaker_label"] == current["speaker_label"]
            or prev["end_ms"] != current["start_ms"]
        ):
            continue
        switch = current["start_ms"]
        # Match the regular activity at the immediate sides of the exclusive switch.
        prior = [
            t
            for t in regular
            if t["speaker_label"] == prev["speaker_label"]
            and t["start_ms"] <= switch - 1 < t["end_ms"]
        ]
        following = [
            t
            for t in regular
            if t["speaker_label"] == current["speaker_label"]
            and t["start_ms"] <= switch < t["end_ms"]
        ]
        if not prior or not following:
            continue
        foreign_end = max(t["end_ms"] for t in prior)
        target_start = min(t["start_ms"] for t in following)
        result.append(
            {
                "switch_ms": switch,
                "from_label": prev["speaker_label"],
                "to_label": current["speaker_label"],
                "regular_previous_end_ms": foreign_end,
                "regular_following_start_ms": target_start,
                "foreign_tail_after_switch_ms": foreign_end - switch,
                "target_lead_before_switch_ms": switch - target_start,
            }
        )
    return result


def sensitivity(event):
    lo, hi = event["range_ms"]
    return [
        {
            "shift_ms": delta,
            "scope": "start boundary sensitivity analysis only",
            "geometry": analyze(
                lo + delta,
                hi,
                event["target_label"],
                event["regular_turns"],
                event["exclusive_turns"],
            ),
        }
        for delta in (-100, -50, -25, 0, 25, 50, 100)
    ]
