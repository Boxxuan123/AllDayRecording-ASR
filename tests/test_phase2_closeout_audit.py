"""Independent behavioural probes against unmodified exported product code."""
from tests.test_phase1_human_facts import people as people
from tests.test_phase2_samples import audio as audio, short_rows, save
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork


def test_accepted_old_aggregate_can_be_retracted_after_new_evidence(audio):
    f, _, provider = audio
    ids=short_rows(f)
    pid=f.core.people.create_person('A')['person_id']
    save(f,ids[:2],pid)
    f.core.people.sample_worker.run_pending()
    old=f.core.people.list_review_candidates(pid)[0]
    f.core.people.review_prototype(old['prototype_id'],pid,'confirmed')
    save(f,ids[2:],pid)
    f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        vectors=u.people.person_vectors(provider.model,provider.model_version)
        assert len(vectors)==1
        print('OLD_ACCEPTED_STILL_MATCHES',len(vectors))
        print('SAMPLE_SETS', [dict(r) for r in u.evidence.connection.execute('SELECT sample_key,prototype_id,current FROM annotation_sample_sets')])
    # Current status is not permission to block withdrawal of an already accepted sample.
    f.core.people.review_prototype(old['prototype_id'],pid,'retracted')
    with SqliteUnitOfWork(f.core.database) as u:
        assert not u.people.person_vectors(provider.model,provider.model_version)


def test_later_long_evidence_can_improve_five_short_windows(audio):
    f, seen, provider=audio
    ids=short_rows(f, ((0,800),(900,1700),(1800,2600),(2700,3500),(3600,4400),(4500,10000)))
    pid=f.core.people.create_person('A')['person_id']
    save(f,ids[:5],pid)
    f.core.people.sample_worker.run_pending()
    assert not f.core.people.list_review_candidates(pid)
    save(f,ids[5:],pid)
    f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        print('WINDOWS_AFTER_LONG_ADDITION',[dict(r) for r in u.evidence.connection.execute('SELECT quality_score,representative_clips_json FROM voice_prototypes')])
    print('EMBEDDED_LENGTHS',seen)
    assert f.core.people.list_review_candidates(pid), '5.5s of later confirmed audio never considered; first five 0.8s clips permanently cap score'


def test_valid_previous_subset_returns_to_review_after_excluding_new_clip(audio):
    from tests.test_phase2_samples import classify
    f, _, provider=audio
    ids=short_rows(f)
    pid=f.core.people.create_person('A')['person_id']
    save(f,ids[:2],pid)
    f.core.people.sample_worker.run_pending()
    first=f.core.people.list_review_candidates(pid)[0]
    save(f,ids[2:],pid)
    f.core.people.sample_worker.run_pending()
    second=f.core.people.list_review_candidates(pid)[0]
    assert first['prototype_id'] != second['prototype_id']
    classify(f,ids[2],'non_speech')
    f.core.people.sample_worker.run_pending()
    with SqliteUnitOfWork(f.core.database) as u:
        print('RESTORED_SUBSET_SETS',[dict(r) for r in u.evidence.connection.execute('SELECT prototype_id,current FROM annotation_sample_sets')])
        print('RESTORED_SUBSET_PLANS',[(p.key,p.windows) for p in u.people.sample_plans(next(iter(u.evidence.connection.execute('SELECT session_id FROM recording_sessions')))[0],provider.model,provider.model_version)[0]])
    assert f.core.people.list_review_candidates(pid), '6s valid previously generated subset permanently hidden as noncurrent'
