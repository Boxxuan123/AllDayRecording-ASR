"""Explainable two-stage local segmentation. No inferred names or outcomes."""
from datetime import datetime
import re

RULE_VERSION = 'daily-local-v1'
HARD_GAP_SECONDS = 300
CONTEXT_GAP_SECONDS = 90
MAX_EVENT_SECONDS = 1200
CANDIDATE_SECONDS = 300
TOPICS = ('汇报', '材料', '项目', '毕业设计', '排期', '同步', '考试', '作业',
          '购物', '买东西', '吃饭', '晚餐', '午餐', '坐车', '出发', '会议')
ACTIVITIES = {'购物': 'purchase', '买东西': 'purchase', '吃饭': 'meal',
              '晚餐': 'meal', '午餐': 'meal', '坐车': 'travel', '出发': 'travel',
              '会议': 'meeting'}


def seconds(value: str) -> float:
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def signals(text: str) -> set[str]:
    return {topic for topic in TOPICS if topic in text} | set(
        re.findall(r'[a-z][a-z0-9_]{3,}', text.lower()))


def candidate_windows(rows: tuple[dict, ...]) -> list[dict]:
    """Short windows preserve gaps, explicit activity/topic and speaker changes."""
    candidates: list[dict] = []
    for row in rows:
        topics = signals(row['text'])
        participant = row['participant']['key']
        current = candidates[-1] if candidates else None
        reason = 'first_source'
        if current:
            gap = seconds(row['start_at']) - seconds(current['evidence'][-1]['end_at'])
            span = seconds(row['end_at']) - seconds(current['evidence'][0]['start_at'])
            conflict = activity_conflict(current['topics'], topics)
            disjoint = bool(topics and current['topics'] and not topics & current['topics'])
            reason = ('temporal_gap' if gap > CONTEXT_GAP_SECONDS else
                      'activity_change' if conflict else
                      'topic_change' if disjoint else
                      'bounded_window' if span > CANDIDATE_SECONDS else
                      'participant_change' if participant not in current['participants'] else '')
        if not current or reason:
            candidates.append({'evidence': [row], 'topics': topics,
                               'participants': {participant}, 'boundary': reason})
        else:
            current['evidence'].append(row)
            current['topics'].update(topics)
            current['participants'].add(participant)
    return candidates


def activity_conflict(left: set[str], right: set[str]) -> bool:
    a = {ACTIVITIES[t] for t in left if t in ACTIVITIES}
    b = {ACTIVITIES[t] for t in right if t in ACTIVITIES}
    return bool(a and b and a.isdisjoint(b))


def normalize_candidates(candidates: list[dict]) -> list[dict]:
    """Merge adjacent windows only; hours-apart same topics remain independent."""
    events: list[dict] = []
    for candidate in candidates:
        previous = events[-1] if events else None
        reason = merge_reason(previous, candidate) if previous else None
        if reason:
            previous['evidence'].extend(candidate['evidence'])
            previous['topics'].update(candidate['topics'])
            previous['participants'].update(candidate['participants'])
            previous['merge_reasons'].append(reason)
        else:
            events.append({**candidate, 'evidence': list(candidate['evidence']),
                           'topics': set(candidate['topics']),
                           'participants': set(candidate['participants']), 'merge_reasons': []})
    return events


def merge_reason(left: dict, right: dict) -> str | None:
    gap = seconds(right['evidence'][0]['start_at']) - seconds(left['evidence'][-1]['end_at'])
    span = seconds(right['evidence'][-1]['end_at']) - seconds(left['evidence'][0]['start_at'])
    if gap > HARD_GAP_SECONDS or span > MAX_EVENT_SECONDS:
        return None
    if activity_conflict(left['topics'], right['topics']):
        return None
    left_tasks = {t for r in left['evidence'] for t in r.get('task_ids', [])}
    right_tasks = {t for r in right['evidence'] for t in r.get('task_ids', [])}
    if left_tasks & right_tasks:
        return 'same_task_within_5_minutes'
    if left['topics'] & right['topics'] and left['participants'] & right['participants']:
        return 'shared_explicit_topic_and_participant_within_5_minutes'
    if gap <= CONTEXT_GAP_SECONDS and span <= CANDIDATE_SECONDS and not (
        left['topics'] and right['topics'] and not left['topics'] & right['topics']
    ):
        # Alternating speakers within continuous speech form a conversation.
        return 'continuous_context_within_90_seconds_bounded_to_5_minutes'
    return None


def meaningful_event(event: dict) -> bool:
    """Exclude isolated fillers without manufacturing facts from recording duration."""
    if event['topics'] or any(r.get('task_ids') for r in event['evidence']):
        return True
    content = []
    for row in event['evidence']:
        text = re.sub(r'[\W_]+', '', row['text'])
        if text and not re.fullmatch(r'[嗯啊哦噢呃唔哼哈]+', text):
            content.append(text)
    return sum(map(len, content)) >= 4


def event_payload(event: dict, day: str, timezone: str) -> dict:
    evidence = event['evidence']
    participants = {r['participant']['key']: r['participant'] for r in evidence}
    tasks = sorted({t for r in evidence for t in r.get('task_ids', [])})
    duration = seconds(evidence[-1]['end_at']) - seconds(evidence[0]['start_at'])
    # Titles are verbatim excerpts, not claims that discussed activities occurred.
    quote = max(evidence, key=lambda r: (bool(signals(r['text'])),
                                        min(64, len(r['text'].strip()))))['text'].strip()
    category = 'task_decision' if tasks else 'conversation'
    salience_reasons = ([] if not tasks else ['linked_existing_task'])
    if len(participants) > 1:
        salience_reasons.append('multiple_source_speakers')
    if duration >= 180:
        salience_reasons.append('sustained_context')
    if len(event['merge_reasons']) > 1:
        salience_reasons.append('repeated_context')
    return {'daily_event_version': RULE_VERSION, 'local_date': day, 'timezone': timezone,
            'start_at': evidence[0]['start_at'], 'end_at': evidence[-1]['end_at'],
            'category': category, 'title': '讨论片段：' + quote[:64],
            'participants': list(participants.values()), 'linked_task_ids': tasks,
            'outcome': 'not_inferred', 'salience': len(salience_reasons),
            'salience_reasons': salience_reasons, 'topics': sorted(event['topics']),
            'evidence_snapshots': evidence,
            'reconciliation': {'boundary': event['boundary'], 'merge_reasons': event['merge_reasons'],
                               'rule_version': RULE_VERSION}}
