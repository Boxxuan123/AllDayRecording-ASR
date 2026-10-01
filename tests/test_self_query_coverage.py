"""Single-speaker ownership, independent windows, and capture-edge recovery."""
import json

import pytest

from tests.test_blind_validation import world as world, seed
from tests.test_self_identity_regression import anchor, frozen, Provider, projection
from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.adapters.sqlite.product_self_queries import product_self_queries


def setup_query(core, session, *, start=1000, end=9000, exclusive=None,
                regular=None, tokens=None, boundary=None, gap=0, invalid_mapping=False):
    with core.database.transaction() as c:
        run, label = c.execute("SELECT run_id,label FROM speaker_tracks WHERE session_id=?",
                               (session,)).fetchone()
        c.execute("UPDATE utterances SET start_ms=?,end_ms=? WHERE utterance_id=?",
                  (start, end, fixtures._utterance_id(1)))
        exclusive = exclusive or [{"speaker_label": label, "start_ms": 0, "end_ms": 10000}]
        for kind, data in {
            "v3_asr_evidence": {"primary_tokens": tokens or [], "hypotheses": []},
            "v3_diarization_evidence": {"exclusive_turns": exclusive, "regular_turns": regular or []},
        }.items():
            raw = json.dumps(data).encode()
            stored = core.artifact_store.put_bytes(raw)
            c.execute("""INSERT INTO artifacts(artifact_id,run_id,kind,producer,producer_version,
              config_digest,input_refs_json,storage_ref,sha256,size_bytes,status,metadata_json,created_at)
              VALUES (?,?,?,'fixture','1',?,'[]',?,?,?,'active','{}',?)""",
              (kind, run, kind, 'a'*64, stored.storage_key, stored.sha256, len(raw), fixtures.NOW_TEXT))
        if boundary is not None:
            # Adapt the shared synthetic seed before exercising the product.
            # Restore its immutable-capture trigger in the same transaction.
            trigger = c.execute("SELECT sql FROM sqlite_master WHERE name='protect_capture_segments_from_update'").fetchone()[0]
            c.execute("DROP TRIGGER protect_capture_segments_from_update")
            c.execute("UPDATE capture_segments SET session_end_ms=?,source_end_ms=? WHERE session_id=?",
                      (boundary, boundary, session))
            c.execute(trigger)
            c.execute("""INSERT INTO capture_segments(segment_id,session_id,asset_id,replica_id,sequence,
              session_start_ms,session_end_ms,source_start_ms,source_end_ms,captured_at,created_at)
              VALUES ('continuation',?,'asset-1','replica-1',1,?,10000,?,?,?,?)""",
              (session, boundary+gap, boundary+gap, 9999 if invalid_mapping else 10000,
               fixtures.NOW_TEXT, fixtures.NOW_TEXT))


def queries(core, session):
    with core.database.read() as c:
        return product_self_queries(c, session, core.paths.artifact_store_path)


class RecordingProvider(Provider):
    def __init__(self, vector=(1., 0., 0.), *, disagree=False):
        super().__init__(vector)
        self.inputs = []
        self.disagree = disagree

    def embed(self, tracks):
        self.inputs.extend(tracks)
        if not self.disagree:
            return super().embed(tracks)
        return tuple(e for i, track in enumerate(tracks)
                     for e in Provider((1.,0.,0.) if i % 2 == 0 else (0.,1.,0.)).embed((track,)))


@pytest.mark.parametrize('case,expected', [
    ('pure', 'self'), ('short', 'unknown'), ('separated', 'unknown'),
    ('overlap', 'unknown'), ('mixed_track', 'self'),
    ('disagree', 'unknown'), ('near_threshold_negative', 'unknown'),
    ('capture_edge', 'self'), ('capture_gap', 'unknown'),
])
def test_product_query_coverage_and_safety(world, case, expected):
    _, core, *_ = world
    session = seed(world)
    options = {}
    if case == 'short':
        options['end'] = 3500
    elif case == 'separated':
        options.update(start=1000, end=7000, exclusive=[
            {'speaker_label':'speaker_01','start_ms':1000,'end_ms':3500},
            {'speaker_label':'foreign','start_ms':3500,'end_ms':4500},
            {'speaker_label':'speaker_01','start_ms':4500,'end_ms':7000}],
            regular=[{'speaker_label':'foreign','start_ms':3500,'end_ms':4500}])
    elif case == 'overlap':
        options['regular'] = [{'speaker_label':'foreign','start_ms':3000,'end_ms':4000}]
    elif case == 'mixed_track':
        options.update(start=4000, end=8000, exclusive=[
            {'speaker_label':'foreign','start_ms':0,'end_ms':4000},
            {'speaker_label':'speaker_01','start_ms':4000,'end_ms':8000},
            {'speaker_label':'foreign','start_ms':8000,'end_ms':10000}])
    elif case in ('capture_edge', 'capture_gap'):
        options.update(boundary=4000, tokens=[{'start_ms':3500,'end_ms':3780}],
                       gap=0 if case == 'capture_edge' else 100)
    setup_query(core, session, **options)
    anchor(core)
    provider = RecordingProvider((.79, .6131, 0.) if case=='near_threshold_negative' else (1.,0.,0.),
                                 disagree=case=='disagree')
    core.people._provider = provider
    core.people.blind_validation = None
    before = frozen(core)
    selected = queries(core, session)[0]
    core.people.analyze(session)
    assert projection(core)[0][0] == expected
    assert frozen(core) == before
    if case == 'capture_edge':
        assert selected['replanning']['reason'] == 'token_boundary_short_tail'
        assert [(w['session_start_ms'],w['session_end_ms']) for w in selected['windows']] == [(1000,4000),(4000,9000)]
        assert sum(c.source_end_ms-c.source_start_ms for t in selected['tracks'] for c in t.clips)==8000
    if case == 'mixed_track':
        clips = [c for t in provider.inputs if t.speaker_track_id.startswith(fixtures._utterance_id(1)) for c in t.clips]
        assert [(c.source_start_ms,c.source_end_ms) for c in clips] == [(4000,6000),(6000,8000)]
    if case in ('separated','overlap','capture_gap'):
        assert not selected['tracks']


def test_short_tail_fallback_retains_invalid_source_mapping_rejection(world):
    _, core, *_ = world
    session = seed(world)
    setup_query(core, session, boundary=4000, tokens=[{'start_ms':3500,'end_ms':3780}],
                invalid_mapping=True)
    selected = queries(core, session)[0]
    assert selected['reason'] == 'incomplete_or_mixed_query'
    assert not selected['tracks']
