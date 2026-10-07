from datetime import timedelta
import json
import pytest

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application.insights import DailyInsightService
from allday_asr.v3.domain.daily_events import candidate_windows, normalize_candidates, meaningful_event
from allday_asr.v3.application.knowledge_support import cascade_derivations
from tests import test_v32_three_layer_knowledge as seed_module

UTTERANCE_ID = seed_module.UTTERANCE_ID
_event_submission = seed_module._event_submission


@pytest.fixture
def source():
    seed = seed_module.V32ThreeLayerKnowledgeTests()
    seed.setUp()
    def factory():
        return SqliteUnitOfWork(seed.database)
    service = DailyInsightService(factory, None, now=lambda: seed.now)
    yield seed, service, factory
    seed.tearDown()


def refresh(source):
    return source[1].refresh_daily('2026-09-01')['objective']


def add(source, minutes=1, text='继续讨论材料', identity='unknown'):
    seed = source[0]
    with seed.database.transaction() as c:
        row = dict(c.execute('SELECT * FROM utterances WHERE utterance_id=?', (UTTERANCE_ID,)).fetchone())
        row.update(utterance_id=f'{minutes:026d}', ordinal=minutes, text=text, original_text=text,
                   identity=identity, start_at=(seed.now + timedelta(minutes=minutes)).isoformat(),
                   end_at=(seed.now + timedelta(minutes=minutes, seconds=1)).isoformat())
        columns = ','.join(row)
        c.execute(f'INSERT INTO utterances ({columns}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    return row['utterance_id']


def test_persistence_evidence_and_idempotency(source):
    first = source[1].refresh_daily('2026-09-01')
    second = source[1].refresh_daily('2026-09-01')
    assert first['revision'] == second['revision'] == 1
    event = first['objective']['events'][0]
    assert event['evidence_snapshots'][0]['utterance_id'] == UTTERANCE_ID
    assert event['evidence_snapshots'][0]['audio_ranges'][0]['start_ms'] == 100
    with source[0].database.read() as c:
        assert c.execute('SELECT count(*) FROM event_operations').fetchone()[0] == 1
        assert c.execute('SELECT count(*) FROM change_events').fetchone()[0] == 2


def test_contiguous_task_speech_groups(source):
    add(source, text='我们讨论项目材料')
    add(source, 2, '继续讨论项目材料')
    assert len(refresh(source)['events']) == 1


def test_long_gap_different_topics(source):
    add(source, 240, '今天买东西')
    assert len(refresh(source)['events']) == 2


def row(minute, topic, session='file-a', key='self'):
    return {'start_at': f'2026-09-01T08:{minute:02}:00+00:00',
            'end_at': f'2026-09-01T08:{minute:02}:10+00:00',
            'text': topic, 'session_id': session, 'participant': {'key': key}, 'task_ids': []}


def test_cross_recording_continuity():
    rows = (row(0, '汇报材料'), row(1, '汇报材料', 'file-b'))
    assert len(normalize_candidates(candidate_windows(rows))) == 1


def test_isolated_fillers_are_not_daily_events():
    events = normalize_candidates(candidate_windows((row(0, '嗯嗯'), row(10, '啊'))))
    assert not any(meaningful_event(e) for e in events)


def test_same_topic_hours_apart(source):
    add(source, 240, '我们明天十点见。')
    assert len(refresh(source)['events']) == 2


def test_existing_task_link_and_current_completion(source):
    seed = source[0]
    receipt = seed.knowledge.submit_generation(_event_submission())
    task = seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], 'reviewer').resource_id
    before = refresh(source)
    assert before['tasks_pending'][0]['task_id'] == task
    update = seed.knowledge.submit_generation(_event_submission(operation='complete', expected_revision=1,
                                                event_id=task, patch={'time': '11:00'}))
    seed.knowledge.accept_proposal(update['proposals'][0]['proposal_id'], 'reviewer')
    after = refresh(source)
    assert after['tasks_completed'][0]['task_id'] == task
    assert not after['tasks_pending']
    with seed.database.read() as c:
        assert c.execute("SELECT count(*) FROM event_current_states WHERE event_kind='appointment'").fetchone()[0] == 1


def test_unknown_never_guesses_name(source):
    people = refresh(source)['people_interacted']
    assert all(p['kind'] == 'unknown' and p['label'] == '未知说话人' for p in people)


def revise(source, text=None, identity=None):
    with source[0].database.transaction() as c:
        c.execute('UPDATE utterances SET text=COALESCE(?,text), identity=COALESCE(?,identity),revision=revision+1 WHERE utterance_id=?',
                  (text, identity, UTTERANCE_ID))
    with source[2]() as uow:
        cascade_derivations(uow, source_type='utterance', source_id=UTTERANCE_ID,
                            source_revision=1, reason='test_correction', now=source[0].now)


def test_user_accepted_task_keeps_its_intent_after_evidence_change(source):
    receipt = source[0].knowledge.submit_generation(_event_submission())
    task_id = source[0].knowledge.accept_proposal(
        receipt['proposals'][0]['proposal_id'], 'reviewer'
    ).resource_id
    with source[2]().reading() as uow:
        before = uow.knowledge.get_event(task_id)
    revise(source, text='更正后的转写')
    with source[2]().reading() as uow:
        task = uow.knowledge.get_event(task_id)
        assert task is not None and task.derivation_status == 'active'
        assert task.status == before.status
        assert task.revision == before.revision and task.payload == before.payload
    with source[0].database.read() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM recompute_requests WHERE target_type='event' AND target_id=?",
            (task_id,),
        ).fetchone()[0] == 0
        assert c.execute(
            """SELECT d.input_revision, u.revision FROM derivation_dependencies d
            JOIN utterances u ON u.utterance_id=d.input_id
            WHERE d.dependent_type='event' AND d.dependent_id=?
              AND d.input_type='utterance'""",
            (task_id,),
        ).fetchone()[:] == (1, 2)
    with source[2]() as uow:
        cascade_derivations(uow, source_type='utterance', source_id=UTTERANCE_ID,
                            source_revision=1, reason='test_correction', now=source[0].now)
    daily = refresh(source)
    assert daily['events'] and all(event['event_id'] != task_id for event in daily['events'])
    with source[2]().reading() as uow:
        task = uow.knowledge.get_event(task_id)
        assert task.derivation_status == 'active'
        assert task.revision == before.revision and task.payload == before.payload


def test_automatic_event_becomes_stale_once_and_is_queued_for_recompute(source):
    receipt = source[0].knowledge.submit_generation(_event_submission())
    event_id = source[0].knowledge.accept_proposal(
        receipt['proposals'][0]['proposal_id'], 'semantic-event-policy'
    ).resource_id
    revise(source, text='自动事件来源更正')
    with source[2]() as uow:
        cascade_derivations(uow, source_type='utterance', source_id=UTTERANCE_ID,
                            source_revision=1, reason='test_correction', now=source[0].now)
    with source[2]().reading() as uow:
        assert uow.knowledge.get_event(event_id).derivation_status == 'stale'
    with source[0].database.read() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM invalidation_events WHERE target_type='event' AND target_id=?",
            (event_id,),
        ).fetchone()[0] == 1
        assert c.execute(
            "SELECT COUNT(*) FROM recompute_requests WHERE target_type='event' AND target_id=?",
            (event_id,),
        ).fetchone()[0] == 1


@pytest.mark.parametrize('actor', ['semantic-event-policy', 'reminder-policy'])
def test_automatic_recomputation_cannot_overwrite_confirmed_task(source, actor):
    receipt = source[0].knowledge.submit_generation(_event_submission())
    task_id = source[0].knowledge.accept_proposal(
        receipt['proposals'][0]['proposal_id'], 'reviewer'
    ).resource_id
    revise(source, text='来源已变化')
    before = source[0].knowledge.list_events(seed_module.SESSION_ID)[0]
    rerun = source[0].knowledge.submit_generation(_event_submission(
        operation='update', expected_revision=1, event_id=task_id,
        patch={'title': '自动覆盖标题'}))
    with pytest.raises(ValueError, match='confirmed task requires a user decision'):
        source[0].knowledge.accept_proposal(rerun['proposals'][0]['proposal_id'], actor)
    after = source[0].knowledge.list_events(seed_module.SESSION_ID)[0]
    assert after['revision'] == before['revision']
    assert after['payload'] == before['payload']
    assert after['derivation_status'] == 'active'


def test_identity_correction_targeted(source):
    add(source, 240, '买东西')
    before = refresh(source)['events']
    revise(source, identity='self')
    after = refresh(source)['events']
    assert [e['event_id'] for e in before] == [e['event_id'] for e in after]
    assert after[0]['participants'][0]['kind'] == 'self'
    assert after[0]['revision'] == 2 and after[1]['revision'] == 1


def test_revision_uses_daily_generation_without_double_invalidation(source):
    first = refresh(source)['events'][0]
    revise(source, text='修正后的项目材料')
    with source[2]().reading() as uow:
        assert uow.knowledge.get_event(first['event_id']).derivation_status == 'active'
    event = refresh(source)['events'][0]
    assert event['revision'] == 2
    assert event['evidence_snapshots'][0]['text'] == '修正后的项目材料'
    with source[0].database.read() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM invalidation_events WHERE target_type='event' AND target_id=?",
            (first['event_id'],),
        ).fetchone()[0] == 0


def test_late_arrival_yesterday_updates_same_ids(source):
    first = refresh(source)['events'][0]
    source[0].now += timedelta(days=1)
    # Evidence timestamp, rather than arrival, determines the summary date.
    source[0].now -= timedelta(days=1)
    add(source)
    source[0].now += timedelta(days=1)
    result = refresh(source)
    assert result['date'] == '2026-09-01'
    assert result['events'][0]['event_id'] == first['event_id']
    assert len(result['events'][0]['evidence_snapshots']) == 2


def test_model_failure_does_not_affect_local_retry(source):
    class Broken:
        def generate_daily(self, *args):
            raise RuntimeError('model unavailable')
    source[1]._generator = Broken()
    from allday_asr.v3.application.insight_errors import InsightGenerationFailed
    with pytest.raises(InsightGenerationFailed):
        source[1].generate_daily('2026-09-01', 'Asia/Singapore')
    assert len(refresh(source)['events']) == 1
    with source[0].database.read() as c:
        assert c.execute('SELECT text FROM utterances').fetchone()[0] == '我们明天十点见。'


def test_manual_fix_survives_automatic_rebuild(source):
    event = refresh(source)['events'][0]
    with source[0].database.transaction() as c:
        raw = c.execute('SELECT payload_json FROM event_current_states').fetchone()[0]
        payload = json.loads(raw) | {'manual_fixed': True, 'title': '人工确认标题'}
        c.execute('UPDATE event_current_states SET payload_json=?', (json.dumps(payload),))
    add(source)
    with source[2]().reading() as uow:
        assert uow.knowledge.get_event(event['event_id']).payload['title'] == '人工确认标题'
    assert refresh(source)['events'][0]['title'] == '人工确认标题'


def test_task_reschedule_reads_current_schedule(source):
    seed = source[0]
    receipt = seed.knowledge.submit_generation(_event_submission())
    task = seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], 'reviewer').resource_id
    refresh(source)
    receipt = seed.knowledge.submit_generation(_event_submission(operation='update', expected_revision=1,
        event_id=task, patch={'scheduled_at': '2026-09-01T09:00:00Z'}))
    seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], 'reviewer')
    assert refresh(source)['tasks_pending'][0]['scheduled_at'] == '2026-09-01T09:00:00Z'


def test_full_reprocessing_reuses_event_id_and_task_source(source):
    seed = source[0]
    receipt = seed.knowledge.submit_generation(_event_submission())
    task = seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], 'reviewer').resource_id
    before = refresh(source)['events'][0]
    with seed.database.transaction() as c:
        row = dict(c.execute('SELECT * FROM utterances WHERE utterance_id=?', (UTTERANCE_ID,)).fetchone())
        c.execute("UPDATE utterances SET status='stale' WHERE utterance_id=?", (UTTERANCE_ID,))
        row.update(utterance_id='00000000000000000000000999', ordinal=999)
        c.execute(f'INSERT INTO utterances ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    after = refresh(source)['events'][0]
    assert after['event_id'] == before['event_id'] and after['revision'] == 2
    assert after['linked_task_ids'] == [task]
