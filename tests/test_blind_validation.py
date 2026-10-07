"""Real SQLite/application boundaries with synthetic audio embeddings only."""

import json
import sqlite3
from dataclasses import replace

import pytest

from tests import test_v34_open_speaker_identity as fixtures
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.dataset_reservations import configure, open_holdout
from allday_asr.v3.adapters.sqlite.blind_admission import admission, CAPTURE_TIME_BLOCK
from allday_asr.v3.adapters.sqlite.blind_queries import automatic_queries, latest_run
from allday_asr.v3.application.blind_scoring import components, digest
from allday_asr.v3.adapters.purity_shadow import build_shadow
from tests.test_enrollment_purity_gate import enrollment_source
from allday_asr.v3.domain.people import PersonKind, RepresentativeClip, SpeakerEmbedding


class Provider:
    model = 'fixture-speaker'
    model_version = '1'
    fail = False

    def embed(self, tracks):
        if self.fail:
            raise RuntimeError('synthetic transient failure')
        return tuple(SpeakerEmbedding(t.speaker_track_id, self.model, self.model_version, (1., 0., 0.),
            tuple(RepresentativeClip(c.media_id, c.source_start_ms, c.source_end_ms, c.utterance_id) for c in t.clips), 1.) for t in tracks)


@pytest.fixture
def world():
    helper = fixtures.V34OpenSpeakerIdentityTests()
    helper.setUp()
    core = helper.core
    provider = Provider()
    service = core.blind_validation
    service.provider = provider
    service.model_verifier = lambda snapshot: None
    service.output_dir = helper.root / 'reports'
    alpha = core.people.create_person('Synthetic A')['person_id']
    beta = core.people.create_person('Synthetic B')['person_id']
    self_id = core.people.create_person('Synthetic Self', PersonKind.SELF)['person_id']
    matcher = {'known_ids': [alpha, beta], 'minimum_speech_seconds': 6.,
        'G': {'threshold': .42, 'margin': .1}, 'P': {'global_fallback': .42, 'per_person': {}, 'margin': .1}}
    snapshot = {'model': provider.model, 'model_version': provider.model_version, 'model_files': {},
        'model_sha256': '0'*64, 'matcher': matcher, 'matcher_version': 'frozen-test-v1', 'GP_version': digest(matcher),
        'legacy': {alpha: [[1,0,0]], beta: [[0,1,0]]}, 'clean': {alpha: [[1,0,0]], beta: [[0,1,0]]},
        'profile_hashes': {'legacy': 'a'*64, 'clean': 'b'*64}, 'self_person_id': self_id,
        'seen_audio_sha256': [], 'data_cutoff': '2026-01-01', 'query_probe_thresholds': {'min_pairwise_cosine': 1.1},
        'evidence_requirements': {'events': 30, 'sessions': 5, 'dates': 3, 'events_per_known': 3,
                                'sessions_per_known': 2, 'unknown_events': 10, 'self_events': 5}}
    service.initialize_experiment('synthetic-v1', 'clean-v1', snapshot)
    yield helper, core, service, provider, alpha, beta, self_id, snapshot
    helper.tearDown()


def seed(world, role='blind', number=1):
    helper, core, *_ = world
    with core.database.transaction() as c:
        configure(c, actor='test', collection=role == 'blind', blind_ratio=0,
                  holdout_ratio=1 if role == 'holdout' else 0, policy_version='fixture-'+role+str(number))
    helper._seed_track(number)
    # Two distinct complete windows, enough duration, probe intentionally suspicious.
    with SqliteUnitOfWork(core.database) as uow:
        row = uow.evidence.get_utterance(fixtures._utterance_id(number))
        uow.desktop.connection.execute('UPDATE utterances SET end_ms=9000 WHERE utterance_id=?', (row.utterance_id,))
        uow.evidence.add_utterance(replace(row, utterance_id='extra-'+str(number), ordinal=1, start_ms=9000, end_ms=10000))
    return fixtures._session_id(number)


@pytest.mark.parametrize('role', ['blind', 'holdout', 'learning'])
def test_role_before_learning_and_late_annotation(world, role):
    _, core, service, _, alpha, *_ = world
    session = seed(world, role)
    with SqliteUnitOfWork(core.database) as uow:
        reserved = uow.desktop.connection.execute('SELECT * FROM session_dataset_roles WHERE session_id=?', (session,)).fetchone()
        assert reserved['dataset_role'] == role
        assert reserved['allocation_ordinal'] == 1
        assert reserved['frozen_at'] is not None if role != 'learning' else reserved['frozen_at'] is None
    if role == 'blind':
        assert service.run_once()
    core.people.assign_utterances([{'utterance_id': fixtures._utterance_id(1), 'revision': 1}], person_id=alpha, display_name=None, actor='test')
    with SqliteUnitOfWork(core.database) as uow:
        uow.people.enqueue_samples(session)
        plans, reason = uow.people.sample_plans(session, 'fixture-speaker', '1')
        if role == 'learning':
            assert plans
            assert uow.people.sample_job(session)['status'] == 'queued'
        else:
            assert not plans and 'dataset_role_excluded' in reason
            assert uow.desktop.connection.execute('SELECT COUNT(*) FROM annotation_sample_queue WHERE session_id=?', (session,)).fetchone()[0] == 0
            assert uow.people.person_vectors('fixture-speaker', '1') == ()
            with pytest.raises(ValueError, match='dataset_role_excluded'):
                uow.people.confirmed_enrollment_input(session, 'newtrack', ((0,8000),))
            with pytest.raises(sqlite3.IntegrityError, match='dataset_role_excluded'):
                uow.desktop.connection.execute("INSERT INTO annotation_sample_sets VALUES('bad',?,?,'fixture-speaker','1','none','[]','[]',1)", (session, alpha))
    if role == 'blind':
        assert service.report()['progress']['generated_events'] == 1
    if role == 'holdout':
        assert not service.run_once()
        with core.database.transaction() as c:
            open_holdout(c, session, 'future-study-v2', 'test', 'synthetic decision')
        assert core.blind_validation.status()['session_roles']['holdout'] == 1


def test_retry_freeze_truth_history_report_and_no_model_hints(world):
    _, core, service, provider, alpha, _, self_id, snapshot = world
    session = seed(world)
    provider.fail = True
    assert service.run_once()
    assert service.status()['shadow_jobs'] == {'failed': 1}
    provider.fail = False
    service.retry(session)
    assert service.run_once()
    assert not service.run_once()
    task = service.tasks()[0]
    task_id = task['source_id']
    assert not any(k in json.dumps(task) for k in ('best_score', 'decisions', 'SUSPICIOUS', 'legacy', 'clean-v1'))
    with core.database.read() as c:
        pred = dict(c.execute('SELECT * FROM blind_prediction_snapshots').fetchone())
        p = json.loads(pred['prediction_json'])
        assert p['query_probe']['status'] == 'SUSPICIOUS'
        assert p['legacy']['decisions']['G'] == alpha
        assert p['clean']['decisions']['G'] == alpha
        assert c.execute('SELECT COUNT(*) FROM blind_events').fetchone()[0] == 1
        assert c.execute('SELECT COUNT(*) FROM blind_review_tasks').fetchone()[0] == 1
    payload = {'review_id': task['review_id'], 'operation_id': 'synthetic-truth', 'action': 'submit',
               'primary_speaker_person_id': self_id, 'primary_speaker_unknown': False, 'unknown_kind': 'none', 'purity': 'mixed_overlap'}
    review = service.submit(task_id, payload, 'test')
    assert pred['prediction_created_at'] < review['reviewed_at']
    assert service.tasks() == []
    assert len(service.tasks(history=True)) == 1
    assert service.submit(task_id, payload, 'test')['review_id'] == review['review_id']
    report = service.report()
    assert report['event_metrics']['legacy_G']['self_to_other'] == 1
    assert report['progress']['independent_events'] == 1
    assert report['status'] == 'INSUFFICIENT BLIND EVIDENCE'
    service.submit(task_id, {'action': 'undo', 'operation_id': 'undo-test'}, 'test')
    assert len(service.tasks()) == 1
    assert service.report()['progress']['independent_events'] == 0
    assert service.initialize_experiment('synthetic-v1', 'clean-v1', snapshot)
    with pytest.raises(ValueError, match='already frozen'):
        service.initialize_experiment('synthetic-v1', 'clean-v1', snapshot | {'clean': snapshot['legacy'], 'GP_version': 'changed'})
    with core.database.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError, match='frozen'):
            c.execute("UPDATE blind_prediction_snapshots SET prediction_json='{}'")
        with pytest.raises(sqlite3.IntegrityError, match='cannot_promote_to_blind'):
            c.execute("UPDATE session_dataset_roles SET dataset_role='learning',role_revision=role_revision+1 WHERE session_id=?", (session,))
    with core.database.read() as c:
        assert dict(c.execute('SELECT * FROM blind_prediction_snapshots').fetchone()) == pred
    for name in ('experiment.json', 'session-roles.json', 'blind-events.json', 'prediction-snapshots.json',
                 'review-progress.json', 'event-level-results.json', 'query-view-diagnostics.json',
                 'current-report.json', 'current-report.md', 'verification.json'):
        assert (service.output_dir/name).exists()


def test_prior_person_truth_blocks_unbiased_prediction(world):
    _, core, service, _, alpha, *_ = world
    session = seed(world)
    core.people.assign_utterances([{'utterance_id': fixtures._utterance_id(1), 'revision': 1}], person_id=alpha, display_name=None, actor='test')
    assert not service.run_once()
    assert service.status()['shadow_jobs'] == {'blocked': 1}
    with core.database.read() as c:
        assert c.execute('SELECT COUNT(*) FROM blind_prediction_snapshots').fetchone()[0] == 0
        assert c.execute('SELECT COUNT(*) FROM voice_prototypes').fetchone()[0] == 0
        assert c.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (session,)).fetchone()[0] == 'blind'


def test_event_transitive_overlap_and_cross_session_copies():
    def q(i, session, track, sha, start, end):
        return {'query_id': str(i), 'session_id': session, 'speaker_track_id': track,
                'session_start_ms': start, 'session_end_ms': end,
                'windows': [{'sha256': sha, 'start_ms': start, 'end_ms': end}]}
    rows = [q(1,'a','t','sha1',0,8000), q(2,'a','t','sha2',9000,17000),
            q(3,'b','other','sha2',10000,18000), q(4,'c','other','sha3',0,8000)]
    groups = components(rows)
    assert sorted(len(r) for r, _ in groups) == [1,3]
    assert {e['reasons'][0] for _, edges in groups for e in edges} == {'track_temporal_proximity', 'original_media_time_overlap'}


def test_database_admission_and_future_clean_builder_fail_closed(world):
    _, core, _, provider, alpha, *_ = world
    seed(world)
    with core.database.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError, match='dataset_role_excluded'):
            c.execute("INSERT INTO voice_prototypes VALUES('bad',?,'missing',?,'accepted','fixture-speaker','1',3,'[1,0,0]','[]',1,1,NULL,NULL,'now')",
                      (fixtures._track_id(1), alpha))
        with pytest.raises(sqlite3.IntegrityError, match='dataset_role_excluded'):
            c.execute("INSERT INTO purity_candidates VALUES('bad','missing',?,?,'enrollment',NULL,'unreviewed',1,'{}','now','now')", (alpha, fixtures._session_id(1)))
        before = c.execute('SELECT next_ordinal FROM dataset_reservation_settings').fetchone()[0]
        c.execute("INSERT INTO recording_sessions SELECT * FROM recording_sessions WHERE session_id=? ON CONFLICT DO NOTHING", (fixtures._session_id(1),))
        assert c.execute('SELECT next_ordinal FROM dataset_reservation_settings').fetchone()[0] == before
        assert c.execute('SELECT COUNT(*) FROM session_role_audit').fetchone()[0] == 1
    for role in ('blind','holdout',None):
        assert build_shadow([enrollment_source() | {'dataset_role': role}], provider) == []


def test_used_learning_cannot_be_promoted_to_blind(world):
    _, core, _, _, alpha, *_ = world
    session = seed(world, 'learning')
    core.people.assign_utterances([{'utterance_id': fixtures._utterance_id(1), 'revision': 1}],
                                 person_id=alpha, display_name=None, actor='test')
    with core.database.transaction() as c:
        with pytest.raises(sqlite3.IntegrityError, match='cannot_promote_to_blind'):
            c.execute("UPDATE session_dataset_roles SET dataset_role='blind',frozen_at='now',role_revision=2 WHERE session_id=?", (session,))


def test_offline_capture_recovers_by_first_admission_without_changing_experiment(world):
    _, core, service, _, _, _, _, _ = world
    session = seed(world)
    with core.database.transaction() as c:
        c.execute("UPDATE recording_sessions SET captured_start='2020-01-01T00:00:00Z' WHERE session_id=?", (session,))
        original = dict(c.execute('SELECT * FROM blind_experiments').fetchone())
        run = latest_run(c, session)
        c.execute("""INSERT INTO blind_shadow_jobs(session_id,experiment_id,status,input_revision,inputs_json,error,updated_at)
            VALUES(?,'synthetic-v1','blocked',?,'[]',?,'prior-block-time')""", (session, run, CAPTURE_TIME_BLOCK))
    assert service.run_once()
    assert len(service.tasks()) == 1
    assert not service.run_once()
    with core.database.read() as c:
        assert dict(c.execute('SELECT * FROM blind_experiments').fetchone()) == original
        prediction = json.loads(c.execute('SELECT prediction_json FROM blind_prediction_snapshots').fetchone()[0])
        evidence = prediction['admission']
        assert evidence['policy_version'] == 'first-admission-after-freeze-v2'
        assert evidence['previous_decision']['reason'] == CAPTURE_TIME_BLOCK
        assert evidence['previously_seen_audio_sha256'] == []
        assert c.execute('SELECT COUNT(*) FROM voice_prototypes').fetchone()[0] == 0


def test_first_admission_still_excludes_old_session_reused_audio_and_learning_exposure(world):
    _, core, service, _, _, _, _, frozen = world
    session = seed(world)
    with core.database.transaction() as c:
        role = dict(c.execute('SELECT * FROM session_dataset_roles WHERE session_id=?', (session,)).fetchone())
        queries = automatic_queries(c, session, 'synthetic-v1', latest_run(c, session))
        _, error = admission(c, session, role | {'role_assigned_at': '2025-01-01T00:00:00Z'}, frozen, queries, None)
        assert error == 'session admission predates experiment'
        reused = frozen | {'seen_audio_sha256': [queries[0]['windows'][0]['sha256']]}
        _, error = admission(c, session, role, reused, queries, None)
        assert error == 'original media already used before experiment'
        c.execute("INSERT INTO session_learning_exposure VALUES(?,'calibration','synthetic','now')", (session,))
    assert not service.run_once()
    assert service.status()['shadow_jobs'] == {'blocked': 1}
    with core.database.read() as c:
        assert c.execute('SELECT COUNT(*) FROM blind_prediction_snapshots').fetchone()[0] == 0


def test_successful_empty_asr_has_visible_blocked_reason(world):
    _, core, service, *_ = world
    session = seed(world)
    with core.database.transaction() as c:
        c.execute('DELETE FROM utterances WHERE session_id=?', (session,))
    assert not service.run_once()
    assert service.status()['shadow_jobs'] == {'blocked': 1}
    with core.database.read() as c:
        assert c.execute('SELECT error FROM blind_shadow_jobs').fetchone()[0] == 'no usable automatic speaker queries'


def test_legacy_queued_job_gets_admission_check_after_upgrade(world):
    _, core, service, *_ = world
    session = seed(world)
    assert service.enqueue(session)
    with core.database.transaction() as c:
        queries = json.loads(c.execute('SELECT inputs_json FROM blind_shadow_jobs').fetchone()[0])
        for query in queries:
            query.pop('admission')
        c.execute('UPDATE blind_shadow_jobs SET inputs_json=?', (json.dumps(queries),))
    assert service.run_once()
    assert len(service.tasks()) == 1
    with core.database.read() as c:
        prediction = json.loads(c.execute('SELECT prediction_json FROM blind_prediction_snapshots').fetchone()[0])
        assert prediction['admission']['policy_version'] == 'first-admission-after-freeze-v2'
