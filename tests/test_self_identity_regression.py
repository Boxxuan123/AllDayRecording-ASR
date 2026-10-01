"""Real product projection and frozen experiment isolation with synthetic anchors."""
import hashlib
import json

import numpy as np
import pytest

from tests.test_blind_validation import world as world, seed
from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher
from allday_asr.v3.bootstrap import compose_v3_core
from allday_asr.v3.config import CodexReminderSettings
from allday_asr.v3.domain.people import RepresentativeClip, SpeakerEmbedding


class Provider:
    model = "FunASR/CAM++"
    model_version = "v1-local"

    def __init__(self, vector=(1., 0., 0.)):
        self.vector = vector
        self.calls = 0

    def embed(self, tracks):
        self.calls += len(tracks)
        return tuple(SpeakerEmbedding(t.speaker_track_id, self.model, self.model_version,
            self.vector, tuple(RepresentativeClip(c.media_id,c.source_start_ms,c.source_end_ms,
            c.utterance_id) for c in t.clips), min(1.,sum(c.source_end_ms-c.source_start_ms
            for c in t.clips)/12000)) for t in tracks)


def anchor(core):
    root = core.paths.state_dir / "identity"
    root.mkdir(exist_ok=True)
    path = root / "fixture-self.npz"
    np.savez(path, embeddings=np.asarray([[1.,0.,0.],[1.,0.,0.]]), centroid=np.asarray([1.,0.,0.]))
    (root / "active-self-identity-policy.json").write_text(json.dumps({
        "accepted": True, "blockers": [], "voiceprint": str(path),
        "voiceprint_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "policy_version": "synthetic-self", "self_threshold": .8, "not_self_threshold": .2,
        "false_accept_rate": 0., "false_reject_rate": 0.}), encoding="utf-8")
    matcher = CalibratedSelfIdentityMatcher(core.paths.state_dir)
    core.people._self_identity_matcher = matcher
    return matcher, path


def evidence(core, session, *, overlap=False):
    with core.database.transaction() as c:
        run = c.execute("SELECT run_id FROM processing_runs WHERE session_id=?", (session,)).fetchone()[0]
        label = c.execute("SELECT label FROM speaker_tracks WHERE run_id=?", (run,)).fetchone()[0]
        for kind, data in {
            "v3_asr_evidence": {"primary_tokens": [], "hypotheses": []},
            "v3_diarization_evidence": {"exclusive_turns": [
                {"speaker_label": label, "start_ms": 0, "end_ms": 10000}],
                "regular_turns": [{"speaker_label": "foreign", "start_ms": 3000, "end_ms": 4000}]
                    if overlap else []},
        }.items():
            raw = json.dumps(data).encode()
            stored = core.artifact_store.put_bytes(raw)
            c.execute("""INSERT INTO artifacts(artifact_id,run_id,kind,producer,producer_version,
              config_digest,input_refs_json,storage_ref,sha256,size_bytes,status,metadata_json,created_at)
              VALUES (?,?,?,'fixture','1',?,'[]',?,?,?,'active','{}',?)""",
              (kind,run,kind,'a'*64,stored.storage_key,stored.sha256,len(raw),fixtures.NOW_TEXT))


def frozen(core):
    with core.database.read() as c:
        tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0].startswith(('blind_','purity_','speaker_purity_')) or r[0] in ('voice_prototypes','voice_prototype_reviews',
                    'annotation_facts','annotation_sample_sets','session_dataset_roles','person_identity_policy_revisions',
                    'session_learning_exposure','annotation_sample_queue')]
        return {t: [tuple(r) for r in c.execute('SELECT * FROM '+t+' ORDER BY rowid')] for t in tables}


def projection(core):
    with core.database.read() as c:
        row = c.execute("SELECT identity,original_identity FROM utterances WHERE utterance_id=?",
                        (fixtures._utterance_id(1),)).fetchone()
        trace = json.loads(c.execute("SELECT policy_json FROM speaker_cluster_runs WHERE producer='product-self-inference' ORDER BY created_at DESC LIMIT 1").fetchone()[0])
    return tuple(row), trace


@pytest.mark.parametrize('role', ['blind','holdout'])
def test_explicit_anchor_product_inference_and_restart_preserve_frozen_state(world, role):
    _, core, shadow, *_ = world
    session = seed(world, role)
    if role == 'blind':
        assert shadow.run_once()
    evidence(core, session)
    matcher, path = anchor(core)
    provider = Provider()
    core.people._provider = provider
    core.people.blind_validation = None
    before = frozen(core)
    assert matcher.status()['reference_count'] == 2
    result = core.people.analyze(session)
    assert result['learning_excluded'] and result['product_inference_executed']
    assert result['product_self_matched_utterance_count'] == 1
    assert provider.calls == 3
    assert projection(core)[0] == ('self','unknown')
    assert frozen(core) == before
    restart = compose_v3_core(core.paths, codex_settings=CodexReminderSettings(enabled=False),
                              speaker_embedding_provider=provider)
    try:
        restart.people.blind_validation = None
        restart.people.analyze(session)
        assert projection(restart)[0] == ('self','unknown')
        assert frozen(restart) == before
        assert path.exists()
    finally:
        restart.close()


@pytest.mark.parametrize('fault', ['negative','missing','invalid','short','mixed','manual','overlap'])
def test_negative_or_unusable_query_does_not_become_self(world, fault):
    _, core, *_ = world
    session = seed(world)
    evidence(core, session, overlap=fault=='overlap')
    matcher, path = anchor(core)
    core.people._provider = Provider((0.,1.,0.) if fault=='negative' else (1.,0.,0.))
    core.people.blind_validation = None
    if fault == 'missing':
        path.unlink()
    elif fault == 'invalid':
        path.write_bytes(b'broken')
    elif fault == 'short':
        with core.database.transaction() as c:
            c.execute("UPDATE utterances SET end_ms=3500 WHERE utterance_id=?",(fixtures._utterance_id(1),))
    elif fault == 'mixed':
        with core.database.transaction() as c:
            c.execute("UPDATE utterances SET end_ms=11000 WHERE utterance_id=?",(fixtures._utterance_id(1),))
    elif fault == 'manual':
        core.corrections.correct_utterance(fixtures.CorrectUtteranceCommand(
            utterance_id=fixtures._utterance_id(1),expected_revision=1,text='测试声音',actor='human',
            identity=fixtures.SelfIdentity.NOT_SELF,change_identity=True))
    before = frozen(core)
    core.people.analyze(session)
    assert projection(core)[0][0] != 'self'
    assert frozen(core) == before
    if fault in ('missing','invalid'):
        assert not matcher.status()['auto_identity_enabled']


def test_non_learning_enrollment_still_rejected(world):
    _, core, *_ = world
    session = seed(world)
    with fixtures.SqliteUnitOfWork(core.database) as uow:
        with pytest.raises(ValueError, match='learning'):
            uow.people.confirmed_enrollment_input(session,fixtures._track_id(1),((1000,9000),))
