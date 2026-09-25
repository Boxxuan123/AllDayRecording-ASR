"""Independent regression checks. Run with run_isolated_tests.py.
Only synthetic sqlite fixtures and a fake embedding provider, no private audio.
"""
from pathlib import Path
import sys
import os
from dataclasses import replace
from unittest.mock import patch
import pytest
ASR_ROOT=Path(os.environ.get('AUDIT_ASR_ROOT', str(Path(__file__).parents[1]))).resolve()
sys.path.insert(0,str(ASR_ROOT/'tests'))
import test_v34_open_speaker_identity as fixtures  # noqa: E402
from test_v34_open_speaker_identity import _utterance_id  # noqa: E402
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork  # noqa: E402
from allday_asr.v3.domain.sound_kind import sound_uses  # noqa: E402

@pytest.fixture
def case():
    c=fixtures.V34OpenSpeakerIdentityTests()
    c.setUp()
    c._seed_track(1)
    original=c.provider.embed
    def embed_manual(tracks):
        for track in tracks:
            c.provider.vectors[track.speaker_track_id]=(1.0,0.0,0.0)
        return original(tracks)
    with patch.object(c.provider,'embed',side_effect=embed_manual):
        try:
            yield c
        finally:
            c.tearDown()

def current(c, uid=None):
    with SqliteUnitOfWork(c.core.database) as u:
        return u.evidence.get_utterance(uid or _utterance_id(1))

def assign(c, name=None, person=None, uid=None):
    row=current(c,uid)
    result=c.core.people.assign_utterances([{'utterance_id':row.utterance_id,'revision':row.revision}],
        person_id=person,display_name=name,actor='human')
    c.core.people.sample_worker.run_pending()
    return result

def classify(c,kind,uid=None):
    row=current(c,uid)
    result=c.core.corrections.classify_segments([{'utterance_id':row.utterance_id,'revision':row.revision}],kind)
    c.core.people.sample_worker.run_pending()
    return result

def rerun(c):
    # Create the repository fixture's new run/artifact/track, then send its
    # utterance through the actual add_utterance restoration path.
    c._seed_reprocessed_track(1)
    uid=_utterance_id(1,reprocessed=True)
    template=current(c,uid)
    with c.core.database.transaction() as db:
        db.execute('DELETE FROM utterances WHERE utterance_id=?',(uid,))
    with SqliteUnitOfWork(c.core.database) as u:
        assert u.evidence.add_utterance(template)
    return current(c,uid)

def accept_first(c,pid):
    candidates=c.core.people.list_review_candidates(pid)
    assert candidates, 'test setup did not generate candidate'
    c.core.people.review_prototype(candidates[0]['prototype_id'],pid,'confirmed')

def vectors(c):
    with SqliteUnitOfWork(c.core.database) as u:
        return u.people.person_vectors(c.provider.model,c.provider.model_version)

@pytest.mark.parametrize('kind',['non_speech','media_speech'])
def test_rerun_preserves_sound_exclusion_without_person(case,kind):
    classify(case,kind)
    row=rerun(case)
    assert not sound_uses(row.evidence)['content_usable'], f'rerun lost exclusion: {row.evidence}'

def test_restoring_eligible_sound_triggers_candidate_creation(case):
    classify(case,'unintelligible')
    p=assign(case,name='Known')
    assert not case.core.people.list_review_candidates(p['person_id'])
    classify(case,'speech')
    candidates=case.core.people.list_review_candidates(p['person_id'])
    assert candidates, 'Known person restored to speech but no candidate is generated'

def test_reassigning_restored_audio_invalidates_old_person_sample(case):
    p1=assign(case,name='First')
    accept_first(case,p1['person_id'])
    assert vectors(case)
    restored=rerun(case)
    assert restored.evidence['person_annotation']['person_id']==p1['person_id']
    assign(case,name='Corrected',uid=restored.utterance_id)
    remaining={pid for pid,_ in vectors(case)}
    assert p1['person_id'] not in remaining, f'old identity remains eligible after same audio was corrected: {remaining}'

def test_unambiguous_latest_correction_wins_on_next_rerun(case):
    assign(case,name='First')
    restored=rerun(case)
    p2=assign(case,name='Corrected',uid=restored.utterance_id)
    template=replace(restored,run_id='future-run',utterance_id='future-utterance',evidence={})
    with SqliteUnitOfWork(case.core.database) as u:
        result=u.evidence._restore_annotation(template)
    assert result.evidence.get('person_annotation',{}).get('person_id')==p2['person_id'], result.evidence


@pytest.mark.parametrize('delivery', ['mobile', 'grouped'])
def test_mobile_split_preserves_aggregate_sample_usability(case, delivery):
    """Real clip selection + duration scoring; synthetic extraction and vectors."""
    import numpy as np
    import soundfile as sf
    from allday_asr.v3.adapters.speaker_embeddings import FunASRSpeakerEmbeddingProvider
    from allday_asr.v3.domain.device_sync import ClientOperation, SyncRequest, PROJECTION_VERSION
    from allday_asr.v3.domain.ids import stable_ulid
    original=current(case)
    with case.core.database.transaction() as db:
        db.execute('DELETE FROM utterances WHERE utterance_id=?',(original.utterance_id,))
    ids=[]
    with SqliteUnitOfWork(case.core.database) as u:
        for i in range(3):
            uid=stable_ulid('audit-short',str(i))
            ids.append(uid)
            u.evidence.add_utterance(replace(original,utterance_id=uid,ordinal=i,
                start_ms=i*3000,end_ms=(i+1)*3000))
    class Backend:
        def extract_speaker_embeddings(self,samples,*,batch_size=8):
            return np.tile(np.array([1.,0.,0.],dtype=np.float32),(len(samples),1))
    provider=FunASRSpeakerEmbeddingProvider(case.core.audio_store,
        backend_factory=Backend,temp_root=case.root/'audit-embedding-temp')
    case.core.people._provider=provider
    def synthesize_clip(source,destination,start_ms,end_ms):
        sf.write(destination,np.zeros((end_ms-start_ms)*16,dtype=np.float32),16000)
        return destination
    pid=case.core.people.create_person('Known')['person_id']
    operations=tuple(ClientOperation(stable_ulid('audit-op',str(i)),'speaker.assign',None,
        {'selections':[{'utterance_id':uid,'revision':1}],
         'person_id':pid}) for i,uid in enumerate(ids))
    with patch('allday_asr.v3.adapters.speaker_embeddings.funasr.extract_clip',side_effect=synthesize_clip):
        if delivery=='mobile':
            response=case.core.mobile_sync.synchronize('device-1',
                SyncRequest(PROJECTION_VERSION,None,operations,500))
            assert all(r.status.value=='applied' for r in response.receipts)
        else:
            case.core.people.assign_utterances([{'utterance_id':uid,'revision':1} for uid in ids],
                person_id=pid,display_name=None,actor='human')
        case.core.people.sample_worker.run_pending()
    with case.core.database.transaction() as db:
        qualities=[r[0] for r in db.execute('SELECT quality_score FROM voice_prototypes')]
    print('SAMPLE_SCORE_PROBE:',delivery,qualities)
    assert case.core.people.list_review_candidates(pid), (
        f'9 seconds of confirmed audio via {delivery}; scores={qualities}; '
        'default minimum quality=0.5; all candidates are hidden')


def test_model_version_upgrade_can_rebuild_confirmed_samples(case):
    p=assign(case,name='Known')
    accept_first(case,p['person_id'])
    old=case.provider.model_version
    case.provider.model_version='audit-v2'
    try:
        case.core.people.process_annotation_samples([{'utterance_id':_utterance_id(1)}])
        case.core.people.sample_worker.run_pending()
        status=case.core.people.annotation_status([_utterance_id(1)])['items'][0]
        assert any(s['model_version']=='audit-v2' for s in status['samples']), status
    finally:
        case.provider.model_version=old


def test_confirmed_reminder_survives_nonsemantic_sound_label_change():
    import test_v33_intelligent_reminders as reminders_fixture
    from allday_asr.v3.application.processing_correction import CorrectionInvalidationService
    c=reminders_fixture.V33IntelligentReminderTests()
    c.setUp()
    try:
        accepted=c._confirmed(reminders_fixture._intent())
        event_id=accepted['reminder']['event_id']
        assert c.reminders.schedule(event_id)['status']=='scheduled'
        # No change to words, time, actor or commitment; only confirm live conversation.
        CorrectionInvalidationService(c.factory,now=lambda:c.current_time).classify_segments(
            [{'utterance_id':reminders_fixture.UTTERANCE_ID,'revision':1}], 'live_speech')
        actual=c.reminders.schedule(event_id)['status']
        assert actual=='scheduled', f'already-confirmed reminder changed to {actual} after sound label only'
    finally:
        c.tearDown()
