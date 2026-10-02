"""Synthetic geometry of the fixed cases; no private audio or human identifiers."""
import json

import pytest

from tests.test_blind_validation import world as world, seed
from tests.test_self_identity_regression import anchor, frozen, projection
from tests.test_self_query_coverage import setup_query, queries, RecordingProvider
from allday_asr.v3.domain.product_query_ownership import owned_ranges, captures_cover_range


def test_case_a_text_group_spans_same_speaker_turns_without_owning_the_gap(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,start=0,end=8000,exclusive=[
        {'speaker_label':'speaker_01','start_ms':55,'end_ms':4426},
        {'speaker_label':'speaker_01','start_ms':5405,'end_ms':9235}])
    anchor(core)
    core.people._provider=RecordingProvider()
    core.people.blind_validation=None
    before=frozen(core)
    query=queries(core,sid)[0]
    assert query['ownership']['selected_ranges']==[[55,4426],[5405,8000]]
    assert [(r['start_ms'],r['end_ms']) for r in query['ownership']['omitted_ranges']]==[(0,55),(4426,5405)]
    clips=[clip for track in query['tracks'] for clip in track.clips]
    assert [(c.source_start_ms,c.source_end_ms) for c in clips]==[(55,2240),(2240,4426),(5405,8000)]
    core.people.analyze(sid)
    assert projection(core)[0]==('self','unknown')
    assert frozen(core)==before


def test_original_case_a_short_owned_tail_is_not_discarded_to_accept_the_text_group(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,start=0,end=9840,exclusive=[
        {'speaker_label':'speaker_01','start_ms':55,'end_ms':4426},
        {'speaker_label':'speaker_01','start_ms':5405,'end_ms':9235},
        {'speaker_label':'speaker_01','start_ms':9623,'end_ms':10000}])
    query=queries(core,sid)[0]
    assert query['reason']=='incomplete_or_mixed_query'
    assert query['ownership']['selected_ranges'][-1]==[9623,9840]
    assert query['exclusions']==[{'start_ms':9623,'end_ms':9840,
                                  'reason':'below_minimum_useful_duration'}]
    assert not query['tracks']


def test_case_b_regular_overlap_75ms_is_not_waived_by_exclusive_attribution(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,start=1000,end=9000,
        exclusive=[{'speaker_label':'foreign','start_ms':467,'end_ms':923},
                   {'speaker_label':'speaker_01','start_ms':923,'end_ms':10000}],
        regular=[{'speaker_label':'foreign','start_ms':467,'end_ms':1075},
                 {'speaker_label':'speaker_01','start_ms':923,'end_ms':10000}])
    query=queries(core,sid)[0]
    assert query['reason']=='overlapping_or_foreign_regular_turn'
    assert not query['tracks']


@pytest.mark.parametrize('duration',[1,75,250,1000])
def test_real_simultaneous_overlap_of_any_duration_rejects(world,duration):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,regular=[{'speaker_label':'foreign','start_ms':4000,'end_ms':4000+duration}])
    assert queries(core,sid)[0]['reason']=='overlapping_or_foreign_regular_turn'


def test_foreign_exclusive_speech_cannot_be_clipped_out_to_claim_a_mixed_text_group(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,exclusive=[
        {'speaker_label':'speaker_01','start_ms':1000,'end_ms':3500},
        {'speaker_label':'foreign','start_ms':3500,'end_ms':4000},
        {'speaker_label':'speaker_01','start_ms':4000,'end_ms':9000}])
    assert not queries(core,sid)[0]['tracks']


def test_touching_half_open_intervals_are_not_foreign_overlap(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,regular=[{'speaker_label':'foreign','start_ms':0,'end_ms':1000},
                                {'speaker_label':'foreign','start_ms':9000,'end_ms':10000}])
    assert queries(core,sid)[0]['reason'] is None


def test_contiguous_capture_mapping_keeps_positive_ownership_coordinates(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,boundary=4000,exclusive=[
        {'speaker_label':'speaker_01','start_ms':1055,'end_ms':5426},
        {'speaker_label':'speaker_01','start_ms':6405,'end_ms':9500}])
    query=queries(core,sid)[0]
    assert query['reason'] is None
    assert [(w['session_start_ms'],w['session_end_ms']) for w in query['windows']]==[(1055,4000),(4000,5426),(6405,9000)]
    assert all(w['start_ms']==w['session_start_ms'] for w in query['windows'])


def test_unowned_capture_gap_is_still_missing_mapping(world):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid,boundary=4000,gap=100,exclusive=[
        {'speaker_label':'speaker_01','start_ms':1000,'end_ms':3500},
        {'speaker_label':'speaker_01','start_ms':4500,'end_ms':9000}])
    assert queries(core,sid)[0]['reason']=='incomplete_or_mixed_query'


@pytest.mark.parametrize('fault',['stale','ambiguous','wrong_session'])
def test_incompatible_product_artifact_never_silently_supplies_turns(world,fault):
    _,core,*_=world
    sid=seed(world)
    setup_query(core,sid)
    with core.database.transaction() as c:
        if fault=='stale':
            trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='protect_artifacts_from_update'").fetchone()
            if trigger:
                c.execute('DROP TRIGGER protect_artifacts_from_update')
            c.execute("UPDATE artifacts SET status='stale' WHERE kind='v3_diarization_evidence'")
            if trigger:
                c.execute(trigger[0])
        else:
            row=dict(c.execute("SELECT * FROM artifacts WHERE kind='v3_diarization_evidence'").fetchone())
            if fault=='ambiguous':
                row['artifact_id']='second-diarization'
                c.execute('INSERT INTO artifacts('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
            else:
                data={'session_id':'another-session','exclusive_turns':[],'regular_turns':[]}
                raw=json.dumps(data).encode()
                stored=core.artifact_store.put_bytes(raw)
                # Simulate an internally valid but wrongly associated artifact.
                trigger=c.execute("SELECT sql FROM sqlite_master WHERE name='protect_artifacts_from_update'").fetchone()
                if trigger:
                    c.execute('DROP TRIGGER protect_artifacts_from_update')
                c.execute("UPDATE artifacts SET storage_ref=?,sha256=?,size_bytes=? WHERE kind='v3_diarization_evidence'",
                          (stored.storage_key,stored.sha256,len(raw)))
                if trigger:
                    c.execute(trigger[0])
    query=queries(core,sid)[0]
    assert query['reason']=='automatic_turn_evidence_unavailable:ValueError'
    assert not query['tracks']


def test_one_millisecond_gap_is_never_filled_and_duplicate_turns_do_not_make_votes():
    turns=[{'speaker_label':'target','start_ms':1000,'end_ms':4000},
           {'speaker_label':'target','start_ms':1000,'end_ms':4000},
           {'speaker_label':'target','start_ms':4001,'end_ms':9000}]
    assert owned_ranges(1000,9000,'target',turns)==[(1000,4000),(4001,9000)]
    assert not captures_cover_range(1000,11000,[{'session_start_ms':0,'session_end_ms':10000,
        'source_start_ms':0,'source_end_ms':10000,'duration_ms':10000}])
