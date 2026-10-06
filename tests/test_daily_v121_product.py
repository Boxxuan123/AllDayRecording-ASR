"""Anonymous product-layer checks: no semantic model, DB writer or real transcript."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

from allday_asr.v3.application.daily_structured_summary import (
    major_priority_reasons, select_major, structured_summary,
)


def event(identifier, *, importance='MEDIUM', value=50, **changes):
    uid = identifier + '-source'
    result = {
        'event_id': identifier, 'revision': 1, 'title': '匿名信息交流', 'summary': '匿名摘要。',
        'importance': importance, 'salience': 5, 'summary_visibility': 'major',
        'start_at': '2026-01-01T08:00:00Z', 'end_at': '2026-01-01T08:01:00Z',
        'timezone': 'UTC', 'participants': [], 'linked_task_ids': [], 'outcome': None,
        'evidence_snapshots': [{'utterance_id': uid, 'revision': 2, 'text': '匿名来源'}],
        'reconciliation': {'information_value': value, 'routine_logistics': False},
    }
    result.update(changes)
    return result


def outcome(value, kind='decision'):
    value['outcome'] = {'kind': kind, 'quote': '采用方案乙',
                        'evidence_utterance_ids': [value['evidence_snapshots'][0]['utterance_id']]}
    return value


def states(values):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [SimpleNamespace(event_id=e['event_id'], revision=e['revision'], payload=e,
                            status=SimpleNamespace(value='active'), derivation_status='active',
                            created_at=now, updated_at=now) for e in values]


def test_linked_task_above_ordinary_long_high_discussion():
    long = event('long', importance='HIGH', value=100, duration_seconds=30000, utterance_count=900)
    task = event('task', value=35, linked_task_ids=['existing-authoritative-task'])
    assert [e['event_id'] for e in select_major([long, task])] == ['task', 'long']


def test_existing_grounded_future_arrangement_above_routine_logistics():
    arrangement = event('arrangement', value=40)
    arrangement['reconciliation']['materialization'] = {
        'materialize': True, 'reason': 'explicit_plan', 'evidence_utterance_ids': ['arrangement-source']}
    routine = event('routine', importance='HIGH', value=100, category='activity')
    assert select_major([routine, arrangement])[0]['event_id'] == 'arrangement'
    assert 'source_backed_explicit_plan' in major_priority_reasons(arrangement)


def test_explicit_decision_above_generic_informational_discussion():
    decision = outcome(event('decision', value=35))
    generic = event('generic', importance='HIGH', value=100)
    assert select_major([generic, decision])[0]['event_id'] == 'decision'


def test_long_low_secondary_stays_outside_major_even_with_an_outcome():
    low = outcome(event('low', importance='LOW', value=100, summary_visibility='secondary',
                        duration_seconds=50000, utterance_count=3000, participant_count=30))
    assert select_major([low]) == []


def test_equal_nonstrong_events_have_stable_chronological_and_id_order():
    a, b, early = event('a'), event('b'), event('early')
    early['start_at'] = '2026-01-01T07:00:00Z'
    assert [e['event_id'] for e in select_major([b, a, early])] == ['early', 'a', 'b']
    assert select_major([early, a, b]) == select_major([b, a, early])


def test_existing_outcome_remains_available_to_product_display_projection():
    original = outcome(event('decision'))
    projected = structured_summary(states([original]), (), '2026-01-01')
    assert projected['events'][0]['outcome'] == original['outcome']
    assert projected['events'][0]['title'] == original['title']
    assert projected['events'][0]['summary'] == original['summary']


def test_null_outcome_does_not_create_new_result_text():
    original = event('null', outcome=None)
    projected = structured_summary(states([original]), (), '2026-01-01')
    assert projected['events'][0]['outcome'] is None
    assert projected['decisions'] == []


def test_question_or_suggestion_without_structured_outcome_is_not_upgraded():
    question = event('question', title='是否采用方案乙？', summary='建议下次考虑方案乙。')
    before = copy.deepcopy(question)
    assert major_priority_reasons(question) == []
    assert structured_summary(states([question]), (), '2026-01-01')['events'][0]['outcome'] is None
    assert question == before


def test_ranking_does_not_change_event_ids_revisions_or_evidence():
    values = [event('ordinary', importance='HIGH'), outcome(event('decision'))]
    before = copy.deepcopy(values)
    select_major(values)
    assert values == before
    projected = structured_summary(states(values), (), '2026-01-01')
    assert projected['source_event_revisions'] == {'ordinary': 1, 'decision': 1}
    assert [e['evidence_snapshots'] for e in projected['events']] == [e['evidence_snapshots'] for e in before]


def test_final_event_set_and_count_unchanged_by_projection():
    values = [event('ordinary'), outcome(event('decision')),
              event('low', importance='LOW', summary_visibility='secondary')]
    projected = structured_summary(states(values), (), '2026-01-01')
    assert projected['statistics']['event_count'] == len(values)
    assert projected['source_event_ids'] == [e['event_id'] for e in values]
    assert len(projected['events']) == 3


def test_first_screen_keeps_strong_medium_even_among_eight_high_events():
    values = [event(str(i), importance='HIGH', value=100) for i in range(8)]
    decision = outcome(event('decision', value=35))
    selected = select_major([*values, decision])
    assert selected[0] is decision
    assert len(selected) == 9  # existing HIGH retention continues


def test_manual_fixed_override_and_high_retention_are_preserved():
    fixed = event('fixed', importance='LOW', summary_visibility='secondary', manual_fixed=True)
    assert select_major([outcome(event('decision')), fixed])[0] is fixed


def test_unknown_outcome_or_plan_reference_cannot_be_a_strong_signal():
    forged = outcome(event('forged'))
    forged['outcome']['evidence_utterance_ids'] = ['missing']
    forged['reconciliation']['materialization'] = {
        'materialize': True, 'reason': 'explicit_plan', 'evidence_utterance_ids': ['missing']}
    assert major_priority_reasons(forged) == []
