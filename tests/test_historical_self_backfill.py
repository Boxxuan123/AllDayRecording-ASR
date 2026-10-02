"""Product corrections and phone review with synthetic evidence; no research fitting."""
import wave
import json

import pytest

from tests.test_blind_validation import world as world, seed
from tests.test_self_identity_regression import Provider, anchor, evidence, frozen
from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.application.historical_self_backfill import HistoricalSelfBackfillService
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService
from allday_asr.v3.domain.ids import new_ulid


def setup(world, duration=9000, role='learning', overlap=False, negative=False):
    _, core, *_ = world
    sid = seed(world, role)
    evidence(core, sid, overlap=overlap)
    anchor(core)
    core.people._provider = Provider((0.,1.,0.) if negative else (1.,0.,0.))
    with core.database.transaction() as c:
        c.execute('UPDATE utterances SET start_ms=0,end_ms=? WHERE utterance_id=?', (duration, fixtures._utterance_id(1)))
        for (key,) in c.execute('SELECT storage_key FROM audio_replicas'):
            path=core.audio_store.path_for(key)
            path.parent.mkdir(parents=True,exist_ok=True)
            with wave.open(str(path),'wb') as audio:
                audio.setparams((1,2,16000,0,'NONE','not compressed'))
                audio.writeframes(b'\x00\x00'*160000)
    return core, sid, HistoricalSelfBackfillService(core.people, core.audio_store)


def target(plan):
    return next(i for i in plan['items'] if i['utterance_id'] == fixtures._utterance_id(1))


@pytest.mark.parametrize('duration,negative,decision', [(8000,False,'AUTO_SELF'), (3500,False,'REVIEW_SELF_CANDIDATE'),
    (2000,False,'REVIEW_SELF_CANDIDATE'),(1999,False,'KEEP_UNKNOWN'),(3999,False,'REVIEW_SELF_CANDIDATE'),
    (4000,False,'AUTO_SELF'),(9000,True,'KEEP_UNKNOWN'),(3500,True,'KEEP_UNKNOWN')])
def test_current_gate_and_single_window_review(world,duration,negative,decision):
    core,sid,service = setup(world,duration,negative=negative)
    before = frozen(core)
    plan = service.dry_run(session_id=sid)
    assert target(plan)['decision'] == decision
    assert frozen(core) == before
    result = service.apply(json.loads(json.dumps(plan)))
    assert service.apply(plan) == result
    assert frozen(core) == before
    with core.database.read() as c:
        row=c.execute('SELECT identity,original_identity FROM utterances WHERE utterance_id=?',(fixtures._utterance_id(1),)).fetchone()
        assert tuple(row) == ('self' if decision=='AUTO_SELF' else 'unknown','unknown')
        assert c.execute('SELECT COUNT(*) FROM historical_self_runs').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['overlap','foreign','ownership','mapping','audio','evidence'])
def test_unsafe_and_missing_evidence_fail_closed(world,fault):
    core,sid,service=setup(world,overlap=fault in {'overlap','foreign'})
    with core.database.transaction() as c:
        if fault=='ownership':
            c.execute('UPDATE utterances SET end_ms=11000 WHERE utterance_id=?',(fixtures._utterance_id(1),))
        if fault=='mapping':
            trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='protect_capture_segments_from_update'").fetchone()[0]
            c.execute('DROP TRIGGER protect_capture_segments_from_update')
            c.execute('UPDATE capture_segments SET session_start_ms=1000 WHERE session_id=?',(sid,))
            c.execute(trigger)
        if fault=='audio':
            key=c.execute('SELECT storage_key FROM audio_replicas LIMIT 1').fetchone()[0]
            core.audio_store.path_for(key).unlink()
        if fault=='evidence':
            key=c.execute("SELECT storage_ref FROM artifacts WHERE kind='v3_diarization_evidence'").fetchone()[0]
            core.artifact_store.path_for(key).unlink()
    item=target(service.dry_run(session_id=sid))
    assert item['decision']=='KEEP_UNKNOWN'
    assert item['reason_code'] != 'LOW_SELF_SCORE'


@pytest.mark.parametrize('identity', ['self','not_self','unknown'])
def test_manual_identity_wins_even_manual_unknown(world,identity):
    core,sid,service=setup(world)
    core.corrections.correct_utterance(fixtures.CorrectUtteranceCommand(
        fixtures._utterance_id(1),1,'human changed', 'human',identity=fixtures.SelfIdentity(identity),change_identity=True))
    # For unknown->unknown there is a text correction only, so explicitly bind a person instead.
    if identity=='unknown':
        core.people.assign_utterances([{'utterance_id':fixtures._utterance_id(1),'revision':2}],
            person_id=world[4],display_name=None,actor='human')
    assert not any(i['utterance_id']==fixtures._utterance_id(1) for i in service.dry_run(session_id=sid)['items'])


@pytest.mark.parametrize('role', ['blind','holdout'])
def test_reserved_cohort_is_never_scored(world,role):
    core,sid,service=setup(world,role=role)
    before=frozen(core)
    plan=service.dry_run(session_id=sid)
    assert not plan['items'] and plan['skips']['FROZEN_EXPERIMENT']==1
    assert core.people._provider.calls==0
    service.apply(plan)
    assert frozen(core)==before


def test_independent_excluded_without_truth_lookup(world):
    core,sid,service=setup(world)
    with core.database.transaction() as c:
        c.execute("INSERT INTO session_speaker_reservations VALUES (?,'independent_evaluation','2026-08-01','2026-08-01T00:00:00Z',NULL,'fixture')",(sid,))
    plan=service.dry_run(session_id=sid)
    assert not plan['items'] and plan['skips']['INDEPENDENT_EVALUATION']==1
    assert core.people._provider.calls==0


def test_manual_change_between_plan_and_apply_rolls_back(world):
    core,sid,service=setup(world)
    plan=service.dry_run(session_id=sid)
    core.corrections.correct_utterance(fixtures.CorrectUtteranceCommand(fixtures._utterance_id(1),1,'test','human',
        identity=fixtures.SelfIdentity.NOT_SELF,change_identity=True))
    with pytest.raises(ValueError,match='changed'):
        service.apply(plan)
    with core.database.read() as c:
        assert c.execute('SELECT COUNT(*) FROM historical_self_runs').fetchone()[0]==0


@pytest.mark.parametrize('action,identity', [('confirm','self'),('reject','not_self'),('uncertain','unknown')])
def test_phone_sync_audio_human_fact_retry_multidevice_and_no_learning(world,action,identity):
    core,sid,service=setup(world,3500)
    plan=service.dry_run(session_id=sid)
    service.apply(plan)
    reviews=DeviceReviewService(core)
    item=next(i for i in reviews.snapshot()['items'] if i['kind']=='self_identity_review')
    sample=item['context']['voice_candidates'][0]
    audio=reviews.audio({'review_id':item['review_id'],'prototype_id':sample['prototype_id']})
    assert audio['complete_sample'] and audio['end_ms']==3500
    before=frozen(core)
    payload={'review_id':item['review_id'],'prototype_id':sample['prototype_id'],'action':action,'operation_id':new_ulid()}
    first=reviews.resolve('a',payload)
    assert first['result']['identity']==identity
    assert reviews.resolve('a',payload)['result']==first['result']
    second=reviews.resolve('b',dict(payload,operation_id=new_ulid(),action='reject' if action=='confirm' else 'confirm'))
    assert second['result']['identity']==identity and second['result']['already_resolved']
    assert frozen(core)==before
    assert not any(i['kind']=='self_identity_review' and i['context']['review_lane']=='primary' for i in reviews.snapshot()['items'])
    assert not any(i['utterance_id']==fixtures._utterance_id(1) for i in service.dry_run(session_id=sid)['items'])
    with core.database.read() as c:
        assert c.execute('SELECT COUNT(*) FROM historical_self_reviews').fetchone()[0]==1
        assert c.execute('SELECT identity FROM utterances WHERE utterance_id=?',(fixtures._utterance_id(1),)).fetchone()[0]==identity


def test_tampered_plan_rejected(world):
    _,sid,service=setup(world)
    plan=service.dry_run(session_id=sid)
    target(plan)['decision']='BAD'
    with pytest.raises(ValueError,match='digest'):
        service.apply(plan)


@pytest.mark.parametrize('gap', [0,100])
def test_backfill_uses_fixed_cross_capture_tail_plan(world,gap):
    from tests.test_self_query_coverage import setup_query
    _,core,*_ = world
    sid=seed(world,'learning')
    setup_query(core,sid,boundary=4000,gap=gap,tokens=[{'start_ms':3500,'end_ms':3780}])
    anchor(core)
    core.people._provider=Provider()
    with core.database.read() as c:
        key=c.execute('SELECT storage_key FROM audio_replicas LIMIT 1').fetchone()[0]
    path=core.audio_store.path_for(key)
    path.parent.mkdir(parents=True,exist_ok=True)
    with wave.open(str(path),'wb') as audio:
        audio.setparams((1,2,16000,0,'NONE','not compressed'))
        audio.writeframes(b'\x00\x00'*160000)
    service=HistoricalSelfBackfillService(core.people,core.audio_store)
    item=target(service.dry_run(session_id=sid))
    assert item['decision']==('AUTO_SELF' if not gap else 'KEEP_UNKNOWN')
    if not gap:
        assert item['inputs']['replanning']['reason']=='token_boundary_short_tail'


def test_enrollment_source_excluded(world):
    core,sid,service=setup(world)
    with core.database.transaction() as c:
        sha=c.execute('SELECT sha256 FROM audio_assets LIMIT 1').fetchone()[0]
        c.execute('INSERT INTO speaker_enrollment_provenance VALUES (?,?,?)',(sha,'a'*64,fixtures.NOW_TEXT))
    plan=service.dry_run(session_id=sid)
    assert not plan['items'] and plan['skips']['ENROLLMENT_SOURCE']==1


def test_review_manual_race_and_operation_conflict(world):
    core,sid,service=setup(world,3500)
    service.apply(service.dry_run(session_id=sid))
    reviews=DeviceReviewService(core)
    item=next(i for i in reviews.snapshot()['items'] if i['kind']=='self_identity_review')
    payload={'review_id':item['review_id'],'prototype_id':item['source_id'],'action':'uncertain','operation_id':new_ulid()}
    reviews.resolve('a',payload)
    with pytest.raises(ValueError,match='reused'):
        reviews.resolve('a',dict(payload,action='confirm'))


def test_review_rechecks_manual_priority(world):
    core,sid,service=setup(world,3500)
    service.apply(service.dry_run(session_id=sid))
    reviews=DeviceReviewService(core)
    item=next(i for i in reviews.snapshot()['items'] if i['kind']=='self_identity_review')
    core.corrections.correct_utterance(fixtures.CorrectUtteranceCommand(fixtures._utterance_id(1),1,'test','human',
        identity=fixtures.SelfIdentity.NOT_SELF,change_identity=True))
    with pytest.raises(ValueError,match='changed'):
        reviews.resolve('a',{'review_id':item['review_id'],'prototype_id':item['source_id'],'action':'confirm','operation_id':new_ulid()})


def test_repeated_physical_audio_never_counts_as_independent_windows(world):
    from tests.test_self_query_coverage import setup_query,queries
    _,core,*_ = world
    sid=seed(world,'learning')
    setup_query(core,sid,boundary=5000)
    with core.database.transaction() as c:
        trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='protect_capture_segments_from_update'").fetchone()[0]
        c.execute('DROP TRIGGER protect_capture_segments_from_update')
        c.execute("UPDATE capture_segments SET source_start_ms=0,source_end_ms=5000 WHERE segment_id='continuation'")
        c.execute(trigger)
    q=queries(core,sid)[0]
    assert q['reason']=='non_independent_source_windows' and not q['tracks']
