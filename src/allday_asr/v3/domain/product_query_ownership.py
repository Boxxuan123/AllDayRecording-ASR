"""Positive acoustic ownership; text group ranges are not continuous voice turns."""


def owned_ranges(start, end, speaker, turns):
    """Intersect with target exclusive turns, omitting every unowned frame.

    Exact adjacency/overlap can be merged. A gap of even one millisecond is
    never filled, and a foreign exclusive intersection invalidates the input.
    Regular overlap is checked separately by the product repository.
    """
    if any(t['speaker_label'] != speaker and t['start_ms'] < end and t['end_ms'] > start
           for t in turns):
        return []
    spans = sorted((max(start, t['start_ms']), min(end, t['end_ms'])) for t in turns
                   if t['speaker_label'] == speaker and t['start_ms'] < end and t['end_ms'] > start)
    result = []
    for lo, hi in spans:
        if result and lo <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], hi))
        else:
            result.append((lo, hi))
    return result


def omitted_ranges(start, end, ranges):
    result, cursor = [], start
    for lo, hi in ranges:
        if cursor < lo:
            result.append({'start_ms': cursor, 'end_ms': lo, 'reason': 'no_target_exclusive_ownership'})
        cursor = hi
    if cursor < end:
        result.append({'start_ms': cursor, 'end_ms': end, 'reason': 'no_target_exclusive_ownership'})
    return result


def captures_cover_range(start, end, captures):
    """Clipping acoustic gaps must not hide missing or invalid source mapping."""
    cursor = start
    for cap in sorted(captures, key=lambda c: (c['session_start_ms'], c['session_end_ms'])):
        lo, hi = cap['session_start_ms'], cap['session_end_ms']
        if hi <= cursor or lo >= end:
            continue
        if (lo > cursor or cap['source_start_ms'] < 0
                or cap['source_end_ms'] > cap['duration_ms']
                or cap['source_end_ms']-cap['source_start_ms'] != hi-lo):
            return False
        cursor = min(end, hi)
        if cursor == end:
            return True
    return False
