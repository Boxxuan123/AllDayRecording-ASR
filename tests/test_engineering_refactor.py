"""Deterministic audit regressions. All data/models/processes are synthetic."""
from contextlib import closing
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture
def semantic_seed():
    from tests.test_v32_semantic_events import V32SemanticEventTests
    seed = V32SemanticEventTests()
    seed.setUp()
    try:
        yield seed
    finally:
        seed.tearDown()


def correction(seed):
    from tests.test_v32_semantic_events import UTTERANCE_ID
    from allday_asr.v3.application import CorrectUtteranceCommand, CorrectionInvalidationService
    CorrectionInvalidationService(seed.factory).correct_utterance(CorrectUtteranceCommand(
        utterance_id=UTTERANCE_ID, expected_revision=1, text='A revised synthetic decision.', actor='test'))


def extractor(seed, callback=None, *, auto=True):
    from tests.test_v32_semantic_events import _FakeSemanticEventGenerator, _decision
    from allday_asr.v3.application import SemanticEventExtractionService
    generator = _FakeSemanticEventGenerator((_decision(),))
    original = generator.generate

    def generate(request, effort):
        if callback:
            callback()
        return original(request, effort)

    generator.generate = generate
    return SemanticEventExtractionService(seed.factory, seed.knowledge, generator, allow_auto_accept=auto)


def test_r1_model_output_cannot_publish_after_real_correction(semantic_seed):
    from tests.test_v32_semantic_events import SESSION_ID
    seed = semantic_seed
    with pytest.raises(ValueError, match='source conflict'):
        extractor(seed, lambda: correction(seed)).extract(SESSION_ID)
    assert not seed.knowledge.list_events(SESSION_ID)
    assert not seed.knowledge.list_proposals()


@pytest.mark.parametrize('actor', ['desktop-user', 'semantic-event-policy'])
def test_r1_pending_proposal_cannot_rebind_new_revision(semantic_seed, actor):
    from tests.test_v32_semantic_events import SESSION_ID
    seed = semantic_seed
    receipt = extractor(seed, auto=False).extract(SESSION_ID)
    correction(seed)
    with pytest.raises(ValueError, match='source conflict'):
        seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], actor)
    assert not seed.knowledge.list_events(SESSION_ID)
    assert seed.knowledge.list_proposals()[0]['status'] == 'pending'


@pytest.mark.parametrize('mutation', ['superseded', 'unavailable', 'context', 'legacy'])
def test_source_status_context_and_legacy_fail_closed(semantic_seed, mutation):
    from tests.test_v32_semantic_events import SESSION_ID
    seed = semantic_seed
    receipt = extractor(seed, auto=False).extract(SESSION_ID)
    with seed.database.transaction() as connection:
        if mutation == 'superseded':
            connection.execute("UPDATE utterances SET status='stale'")
        elif mutation == 'unavailable':
            connection.execute("UPDATE recording_sessions SET tombstoned_at='2026-10-01T00:00:00Z'")
        elif mutation == 'context':
            connection.execute("UPDATE speaker_tracks SET label='changed-context'")
        else:
            connection.execute("UPDATE generation_records SET input_scope_json=json_remove(input_scope_json,'$.input_snapshot')")
    with pytest.raises(ValueError, match='snapshot|conflict|unavailable'):
        seed.knowledge.accept_proposal(receipt['proposals'][0]['proposal_id'], 'desktop-user')
    with seed.database.read() as connection:
        assert connection.execute('SELECT COUNT(*) FROM event_operations').fetchone()[0] == 0


def test_unrelated_update_does_not_conflict_and_dependencies_are_original(semantic_seed):
    from tests.test_v32_semantic_events import SESSION_ID, UTTERANCE_ID
    seed = semantic_seed
    receipt = extractor(seed, auto=False).extract(SESSION_ID)
    with seed.database.transaction() as connection:
        connection.execute("UPDATE devices SET name='Unrelated device name'")
    pid = receipt['proposals'][0]['proposal_id']
    seed.knowledge.accept_proposal(pid, 'reviewer')
    evidence = seed.knowledge.list_events(SESSION_ID)[0]['evidence']
    assert evidence[0]['evidence_id'] == UTTERANCE_ID
    assert evidence[0]['evidence_revision'] == 1
    with pytest.raises(ValueError, match='no longer pending'):
        seed.knowledge.accept_proposal(pid, 'reviewer')


def test_stale_semantic_event_can_be_regenerated_without_duplicate_identity(semantic_seed):
    from tests.test_v32_semantic_events import SESSION_ID
    seed = semantic_seed
    service = extractor(seed)
    service.extract(SESSION_ID)
    first = seed.knowledge.list_events(SESSION_ID)[0]
    correction(seed)
    ledger = seed.knowledge.list_recompute_requests('queued')
    assert ledger[0]['execution_mode'] == 'manual'
    assert ledger[0]['action_required'] == 'regenerate_from_current_sources'
    service.extract(SESSION_ID)
    events = seed.knowledge.list_events(SESSION_ID)
    assert len(events) == 1 and events[0]['event_id'] == first['event_id']
    assert events[0]['revision'] == 2 and events[0]['derivation_status'] == 'active'
    assert service.extract(SESSION_ID)['duplicate_count'] == 1
    assert seed.knowledge.list_recompute_requests('succeeded')


def test_r2_same_session_new_revision_after_batch_deadline(tmp_path):
    from tests.test_v32_semantic_events import _FakeCodex, _request_utterance
    from allday_asr.v3.adapters.codex import CodexSemanticEventGenerator
    from allday_asr.v3.ports.event_generation import SemanticEventModelRequest, SemanticEventReasoningEffort
    client = _FakeCodex({"events": []})
    generator = CodexSemanticEventGenerator(tmp_path / 'work', codex_factory=lambda: client)
    request = SemanticEventModelRequest('same-session', 'UTC', (_request_utterance(),))
    with patch('allday_asr.v3.adapters.codex.model_execution_runner.time.time', return_value=1000):
        generator.generate(request, SemanticEventReasoningEffort.LOW)
    revised = replace(request, utterances=({**request.utterances[0], 'revision': 2},))
    with patch('allday_asr.v3.adapters.codex.model_execution_runner.time.time', return_value=3000):
        generator.generate(revised, SemanticEventReasoningEffort.LOW)
    batches = list(generator._runner.receipt_dir.glob('batch-*.json'))
    assert len(batches) == 2


def test_execution_normalizes_time_persists_budget_and_explicit_regeneration(tmp_path):
    from tests.test_model_execution_runner import call, successful_result
    from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionRunner, ModelExecutionTerminal
    runner = ModelExecutionRunner(tmp_path / 'receipts', max_attempts=1)
    options = {'scope_execution': True, 'prompt': 'source\n{"now_utc":"first","utterances":[1]}'}
    with patch.object(runner, '_owned_call', side_effect=TimeoutError):
        with pytest.raises(TimeoutError):
            call(runner, **options)
    restarted = ModelExecutionRunner(runner.receipt_dir)
    jitter = {**options, 'prompt': 'source\n{"utterances":[1],"now_utc":"later"}'}
    with pytest.raises(ModelExecutionTerminal):
        call(restarted, **jitter)
    with patch.object(restarted, '_owned_call', return_value=successful_result()) as remote:
        first = call(restarted, **jitter, execution_id='explicit-new-execution')
        cached = call(restarted, **jitter, execution_id='explicit-new-execution')
        assert first.id == cached.id and first.execution_id == cached.execution_id
        assert remote.call_count == 1
    provenance = json.loads((runner.receipt_dir / f'execution-{first.execution_id}.json').read_text())
    assert provenance['explicit_id'] == 'explicit-new-execution'
    states = [json.loads(p.read_text()) for p in runner.receipt_dir.glob('*.job.json')]
    assert any(s.get('execution_id') == first.execution_id for s in states)
    assert len(list(runner.receipt_dir.glob('execution-*.json'))) == 2


def state_store(tmp_path):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'automation')
    store.save('session', {'session_id': 'session', 'status': 'needs_attention', 'input_revision': 1})
    return store


def test_r4_unacknowledged_request_survives_consume_and_restart(tmp_path):
    store = state_store(tmp_path)
    first = store.request_retry('session')
    request = store.consume_retry_requests()
    restarted = type(store)(store.root)
    assert restarted.consume_retry_requests() == request
    assert restarted.request_retry('session')['requested_at'] == first['requested_at']


@pytest.mark.parametrize('crash', ['before_schedule', 'after_schedule', 'after_execute'])
def test_retry_crash_points_and_active_session_do_not_drop_or_repeat(tmp_path, crash):
    store = state_store(tmp_path)
    request = store.request_retry('session')
    store.consume_retry_requests()
    if crash != 'before_schedule':
        store.save('session', {**store.status('session'), 'status': 'queued', 'retry_request_id': request['request_id']})
    if crash == 'after_execute':
        store.save('session', {**store.status('session'), 'status': 'completed'})
    from allday_asr.v3.adapters.transfer.automation import V3AutomaticWorkflowRunner
    runner = object.__new__(V3AutomaticWorkflowRunner)
    runner._states, runner._lock = type(store)(store.root), threading.RLock()
    runner._active, runner._closed, runner._queue = {'session'}, False, queue.Queue()
    runner._input_revision = lambda _: 1
    runner.max_auto_retries = 3
    runner._poll_persistent_work()
    assert runner._queue.empty()
    if crash == 'before_schedule':
        assert store.consume_retry_requests()
        runner._active.clear()
        runner._poll_persistent_work()
        assert runner._queue.get_nowait() == 'session'
        assert store.status('session')['manual_retry_count'] == 1
    assert not store.consume_retry_requests()
    runner._poll_persistent_work()
    assert runner._queue.empty()


def test_legacy_retry_requested_without_file_is_recovered(tmp_path):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.root.mkdir()
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'retry_requested'}))
    assert store.consume_retry_requests()[0]['session_id'] == 'session'
    request = store.consume_retry_requests()[0]
    store.save('session', {'session_id': 'session', 'status': 'queued', 'retry_request_id': request['request_id']})
    assert not type(store)(store.root).consume_retry_requests()


def test_fup02_migrated_json_does_not_duplicate_new_retry(tmp_path):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.root.mkdir()
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'needs_attention'}))
    store.initialize()
    requested = store.request_retry('session')
    pending = store.consume_retry_requests()
    assert len(pending) == 1
    assert pending[0]['request_id'] == requested['request_id']


def retry_runner(store):
    from allday_asr.v3.adapters.transfer.automation import V3AutomaticWorkflowRunner
    runner = object.__new__(V3AutomaticWorkflowRunner)
    runner._states, runner._lock = store, threading.RLock()
    runner._active, runner._closed, runner._queue = set(), False, queue.Queue()
    runner._input_revision = lambda _: 1
    runner.max_auto_retries, runner.poll_interval = 3, .01
    return runner


def test_fup02_complete_poll_execution_and_next_legitimate_retry(tmp_path):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.root.mkdir()
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'needs_attention'}))
    store.initialize()
    requested = store.request_retry('session')
    for _ in range(3):
        store.initialize()
        store.statuses()
        assert len(type(store)(store.root).consume_retry_requests()) == 1
    runner = retry_runner(store)
    calls = []
    def process(sid):
        calls.append(sid)
        runner._progress(sid, 'completed', 'completed', 'synthetic complete')
        runner._queue.put(None)
    runner._process = process
    runner._run_loop()
    assert calls == ['session'] and not runner._active and runner._queue.empty()
    runner._poll_persistent_work()
    assert runner._queue.empty()
    assert store.status('session')['manual_retry_count'] == 1
    assert store.retry_request_report('session')['requests'][0]['status'] == 'scheduled'
    store.save('session', {**store.status('session'), 'status': 'needs_attention'})
    next_request = type(store)(store.root).request_retry('session')
    assert next_request['request_id'] != requested['request_id']
    runner._run_loop()
    assert calls == ['session', 'session']
    assert store.status('session')['manual_retry_count'] == 2


def subprocess_retry_action(args):
    from pathlib import Path
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    root, action = args
    store = AutomaticWorkflowStateStore(Path(root))
    store.initialize()
    if action == 'request':
        return store.request_retry('session')['request_id']
    runner = retry_runner(store)
    runner._poll_persistent_work()
    return int(not runner._queue.empty())


def test_fup02_cross_process_initialize_request_and_schedule_are_unique(tmp_path):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    store = state_store(tmp_path)
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'needs_attention'}))
    with ProcessPoolExecutor(4, mp_context=multiprocessing.get_context('spawn')) as pool:
        requests = list(pool.map(subprocess_retry_action, [(str(store.root), 'request')] * 8))
        scheduled = list(pool.map(subprocess_retry_action, [(str(store.root), 'schedule')] * 8))
    assert len(set(requests)) == 1 and sum(scheduled) == 1
    assert store.status('session')['manual_retry_count'] == 1


@pytest.mark.parametrize('with_state', [False, True])
def test_fup02_orphan_legacy_request_and_no_reactivation(tmp_path, with_state):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.requests_root.mkdir(parents=True)
    if with_state:
        store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'retry_requested'}))
    store._request_path('session').write_text(json.dumps({'session_id': 'session', 'requested_at': '2026-10-01T00:00:00Z'}))
    requests = store.consume_retry_requests()
    assert len(requests) == 1
    store.save('session', {'session_id': 'session', 'status': 'queued', 'retry_request_id': requests[0]['request_id']})
    store.save('session', {'session_id': 'session', 'status': 'completed'})
    assert not type(store)(store.root).consume_retry_requests()


@pytest.mark.parametrize('after_mark', [False, True])
def test_fup02_import_marker_and_request_roll_back_together(tmp_path, monkeypatch, after_mark):
    import sqlite3
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.root.mkdir()
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'retry_requested'}))
    original = store._mark_import
    def fail(*args):
        if after_mark:
            original(*args)
        raise OSError('injected import persistence fault')
    with monkeypatch.context() as patcher:
        patcher.setattr(store, '_mark_import', fail)
        with pytest.raises(OSError):
            store.initialize()
    with closing(sqlite3.connect(store.root / 'workflow-state.sqlite3')) as c, c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 0
    assert len(store.consume_retry_requests()) == 1
    assert len(store.consume_retry_requests()) == 1


@pytest.mark.parametrize('eligible', [True, False])
def test_fup02_repair_only_proven_unclaimed_legacy_duplicate(tmp_path, eligible):
    from allday_asr.v3.adapters.transfer.automation_state import AutomaticWorkflowStateStore, _key
    store = AutomaticWorkflowStateStore(tmp_path / 'legacy')
    store.root.mkdir()
    store._status_path('session').write_text(json.dumps({'session_id': 'session', 'status': 'needs_attention'}))
    request = store.request_retry('session')
    legacy = 'legacy:' + _key('session' + request['requested_at'])
    with store._transaction() as c:
        c.execute("INSERT INTO retry_requests(request_id,session_id,requested_at,status,origin) VALUES (?,?,?,'pending',?)",
                  (legacy, 'session', request['requested_at'], None if eligible else 'legacy-file'))
    pair = store.retry_request_report('session')['duplicate_pairs'][0]
    assert pair['can_merge'] is eligible
    if not eligible:
        with pytest.raises(ValueError, match='cannot be proved'):
            store.coalesce_legacy_duplicate('session', legacy, request['request_id'], 'operator')
        assert len(store.consume_retry_requests()) == 2
        return
    assert store.coalesce_legacy_duplicate('session', legacy, request['request_id'], 'operator')['merged']
    assert not store.coalesce_legacy_duplicate('session', legacy, request['request_id'], 'operator')['merged']
    assert len(store.consume_retry_requests()) == 1
    with store._transaction() as c:
        assert c.execute('SELECT count(*) FROM retry_request_audit').fetchone()[0] == 1


def test_concurrent_retry_entry_points_share_one_durable_request(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = state_store(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        requests = list(pool.map(lambda _: type(store)(store.root).request_retry('session'), range(16)))
    assert len({r['request_id'] for r in requests}) == 1
    assert len(store.consume_retry_requests()) == 1


def test_schema_29_upgrade_repeat_checksum_and_actual_migration_rollback(tmp_path):
    import sqlite3
    from allday_asr.v3.adapters.sqlite import V3MigrationRunner, V3Migration
    from allday_asr.v3.adapters.sqlite.migrations import MIGRATIONS
    path = tmp_path / 'upgrade.sqlite3'
    V3MigrationRunner(path, migrations=MIGRATIONS[:29]).initialize()
    broken = V3Migration(30, 'injected_failure', MIGRATIONS[29].sql + '\nSELECT missing_function();')
    with pytest.raises(sqlite3.OperationalError):
        V3MigrationRunner(path, migrations=(*MIGRATIONS[:29], broken)).initialize()
    with closing(sqlite3.connect(path)) as c, c:
        assert c.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0] == 29
        assert 'queued_at' not in {r[1] for r in c.execute('PRAGMA table_info(annotation_sample_queue)')}
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='daily_dirty_annotated_person_update'").fetchone()
    runner = V3MigrationRunner(path)
    assert runner.initialize() == runner.initialize() == 30
    with closing(sqlite3.connect(path)) as c, c:
        assert 'queued_at' in {r[1] for r in c.execute('PRAGMA table_info(annotation_sample_queue)')}
        assert c.execute('SELECT sha256 FROM schema_migrations WHERE version=30').fetchone()[0] == MIGRATIONS[29].sha256
    with pytest.raises(RuntimeError, match='newer'):
        V3MigrationRunner(path, migrations=MIGRATIONS[:29]).initialize()


def test_corrupt_state_has_diagnostic_and_request_transaction_rolls_back(tmp_path, caplog):
    store = state_store(tmp_path)
    with patch.object(store, '_transaction', side_effect=OSError('disk failure')):
        with pytest.raises(OSError):
            store.request_retry('session')
    assert store.status('session')['status'] == 'needs_attention'
    store._status_path('invalid').write_text('broken-json')
    with pytest.raises(ValueError, match='corrupt'):
        store.initialize()
    assert 'workflow state read failed' in caplog.text


def test_diagnostic_correlation_classification_without_sensitive_messages(caplog):
    from allday_asr.v3.application.failure_diagnostics import failure_category, log_failure
    from allday_asr.v3.adapters.codex.model_execution_runner import ModelExecutionTerminal
    try:
        raise ModelExecutionTerminal('SENSITIVE_PROMPT_PLACEHOLDER')
    except ModelExecutionTerminal as inner:
        try:
            raise RuntimeError('SENSITIVE_TRANSCRIPT_PLACEHOLDER') from inner
        except RuntimeError as wrapped:
            assert failure_category(wrapped) == ('budget_terminal', False)
            log_failure(logging.getLogger('engineering'), wrapped, request_id='correlated', job_id='job', attempt=2)
    assert 'correlated' in caplog.text and 'ModelExecutionTerminal' in caplog.text
    assert 'raise RuntimeError' not in caplog.text
    assert 'SENSITIVE_' not in caplog.text


def test_daily_inputs_query_count_is_bounded_and_fingerprint_stable(semantic_seed):
    from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
    from allday_asr.v3.application.daily_event_inputs import daily_inputs
    from allday_asr.v3.domain.hashing import canonical_json_sha256
    seed = semantic_seed
    def read():
        with SqliteUnitOfWork(seed.database).reading() as uow:
            statements = []
            uow.insights.connection.set_trace_callback(statements.append)
            values = daily_inputs(uow, '2026-08-31T00:00:00Z', '2026-09-02T00:00:00Z')
        return values, len([s for s in statements if s.lstrip().upper().startswith('SELECT')])
    first, small = read()
    # Captured independently from 87532be with this fixed synthetic seed.
    assert canonical_json_sha256(first) == '1f85d17ebd3813d95f26cc4822539693a241d43b1b75451989eee4b821e90dea'
    assert canonical_json_sha256(first) == canonical_json_sha256(read()[0])
    with seed.database.transaction() as connection:
        row = dict(connection.execute('SELECT * FROM utterances LIMIT 1').fetchone())
        columns = ','.join(row)
        for i in range(100):
            copied = {**row, 'utterance_id': f'synthetic-extra-{i}', 'ordinal': i + 1}
            connection.execute(f"INSERT INTO utterances ({columns}) VALUES ({','.join('?' for _ in row)})", tuple(copied.values()))
    large, count = read()
    assert len(large[0]) == 101
    assert count <= small + 1
    assert first[0][0]['audio_ranges'] == large[0][0]['audio_ranges']


@pytest.mark.parametrize('days', [7, 90, 365])
def test_r3_real_composition_sync_never_refreshes_all_history(semantic_seed, days):
    from allday_asr.v3.bootstrap import compose_v3_core, V3CorePaths
    from allday_asr.v3.config import CodexReminderSettings
    from allday_asr.v3.domain.device_sync import SyncRequest, ClientOperation, PROJECTION_VERSION
    from tests.test_v32_semantic_events import UTTERANCE_ID, SPEAKER_ID
    seed = semantic_seed
    core = compose_v3_core(V3CorePaths.from_state_dir(seed.root), codex_settings=CodexReminderSettings(enabled=False))
    person = core.people.create_person('Synthetic history participant')['person_id']
    with seed.database.transaction() as connection:
        source = {table: dict(connection.execute(f'SELECT * FROM {table} LIMIT 1').fetchone())
                  for table in ('recording_sessions', 'processing_runs', 'speaker_tracks', 'utterances')}
        for i in range(1, days):
            started = seed.current_time - timedelta(days=i)
            sid, runid, trackid = f'history-{i}', f'history-run-{i}', f'history-track-{i}'
            for table in source:
                row = dict(source[table])
                for key, value in [('session_id', sid), ('run_id', runid), ('speaker_track_id', trackid), ('utterance_id', f'history-utterance-{i}')]:
                    if key in row:
                        row[key] = value
                for key in ('created_at', 'updated_at', 'captured_start', 'start_at'):
                    if key in row:
                        row[key] = started.isoformat()
                for key in ('captured_end', 'end_at'):
                    if key in row:
                        row[key] = (started + timedelta(seconds=1)).isoformat()
                if 'legacy_ref' in row:
                    row['legacy_ref'] = None
                if table == 'utterances':
                    row['evidence_json'] = json.dumps({'person_annotation': {'person_id': person}})
                connection.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", tuple(row.values()))
    from tests.test_v33_intelligent_reminders import _intent, _submission
    for i in range(1, days):
        intent = replace(_intent(), session_id=f'history-{i}', evidence_utterance_ids=(f'history-utterance-{i}',),
                         title=f'Synthetic historical task {i}', related_person_ids=(person,))
        submission = replace(_submission(intent), input_scope={'session_id': intent.session_id})
        receipt = core.reminders.submit_generation(submission)
        core.reminders.confirm(receipt['candidates'][0]['candidate_id'], 'test-user')
    with seed.database.transaction() as connection:
        connection.execute('DELETE FROM daily_dirty_ranges')
    refreshed = []
    with patch.object(core.insights, 'refresh_daily', side_effect=lambda day, *a, **kw: refreshed.append(day) or {'semantic_status': 'pending'}):
        operation = ClientOperation('real-correction', 'utterance.correct', 1,
            {'utterance_id': UTTERANCE_ID, 'text': 'Changed synthetic source.', 'speaker_track_id': SPEAKER_ID, 'identity': 'unknown'})
        result = core.mobile_sync.synchronize('computer-1', SyncRequest(PROJECTION_VERSION, None, (operation,), 1))
        assert result.receipts[0].status.value == 'applied'
        assert not refreshed
        # Restart/no notify: durable dirty rows are sufficient for the owner.
        core.daily_automation.catch_up('restart-without-notify')
        assert len(set(refreshed)) <= 3 and '2026-09-01' in refreshed
        assert not core.daily_automation.queue.states()
        cursor = result.next_cursor
        for _ in range(2):
            cursor = core.mobile_sync.synchronize('computer-1', SyncRequest(PROJECTION_VERSION, cursor, (), 1)).next_cursor
    core.close()


def test_sample_lease_renewal_fences_expiry_owner_generation_and_exhaustion(semantic_seed):
    from tests.test_v32_semantic_events import SESSION_ID
    from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
    seed = semantic_seed
    with seed.database.transaction() as connection:
        assert connection.execute('SELECT dataset_role FROM session_dataset_roles WHERE session_id=?', (SESSION_ID,)).fetchone()[0] == 'learning'
        connection.execute('INSERT OR REPLACE INTO annotation_sample_queue(session_id) VALUES(?)', (SESSION_ID,))
    with SqliteUnitOfWork(seed.database) as uow:
        job = uow.people.claim_sample_job(1000, 'first')
        assert job
        assert uow.people.renew_sample_job(SESSION_ID, job['generation'], 'first', 1100)
        assert uow.people.claim_sample_job(1150, 'other') is None
        assert not uow.people.renew_sample_job(SESSION_ID, job['generation'], 'first', 1300)
        newer = uow.people.claim_sample_job(1300, 'second')
        assert newer
        assert not uow.people.renew_sample_job(SESSION_ID, job['generation'], 'first', 1301)
        uow.people.finish_sample_job(SESSION_ID, job['generation'], 'first', 'processed', '', 1301)
        assert uow.people.sample_job(SESSION_ID)['token'] == 'second'


def synthetic_native_call(payload, control):
    """Only child processes spawned by the test are touched."""
    import subprocess
    if payload.get('child'):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    else:
        child = None
    Path(payload['marker']).write_text(json.dumps({'pid': os.getpid(), 'child': child.pid if child else None}))
    control.heartbeat({'phase': payload.get('phase', 'model_load')})
    for i in range(payload.get('ticks', 0)):
        time.sleep(0.1)
        control.heartbeat({'phase': 'window', 'window': i})
    if payload.get('hang'):
        time.sleep(60)
    return {'checkpoint': 'finished'}


def synthetic_staged_artifact(payload, control):
    from allday_asr.v3.adapters.files import ContentAddressedStore
    from allday_asr.v3.ports.processing import StageArtifactOutput, StageExecutionResult
    control.heartbeat({'phase': 'artifact_write'})
    data = b'{"synthetic": true}'
    content = ContentAddressedStore(Path(payload['artifacts'])).put_bytes(data)
    return StageExecutionResult(artifacts=(StageArtifactOutput('synthetic_artifact', data, 'test', '1'),),
                                stored_artifacts=(content,))


@pytest.mark.skipif(sys.platform != 'win32', reason='Exclusive Windows Job')
def test_owned_artifact_staging_publishes_metadata_without_parent_file_work(tmp_path):
    from tests.test_v3_durable_processing import V3DurableProcessingTests
    from allday_asr.v3.adapters.models.isolated_native import run_owned_stage
    seed = V3DurableProcessingTests()
    seed.setUp()
    try:
        seed.service.submit(seed._command())
        claim = seed.service.claim('staging-test', {})
        control = SimpleNamespace(heartbeat=lambda _: False, cancellation_requested=lambda: False)
        result = run_owned_stage(__name__ + ':synthetic_staged_artifact', {'artifacts': str(seed.artifact_store.root)},
            control, threading.Event(), tmp_path, stage_seconds=5, progress_seconds=3)
        forged = replace(result, stored_artifacts=(replace(result.stored_artifacts[0], sha256='0' * 64),))
        with pytest.raises(ValueError, match='identity mismatch'):
            seed.service.complete(claim, forged)
        with patch.object(seed.artifact_store, 'put_bytes', side_effect=AssertionError('parent must not stage')):
            seed.service.complete(claim, result)
        with seed.database.read() as c:
            assert c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 1
        assert seed.artifact_store.path_for(result.stored_artifacts[0].storage_key).read_bytes() == result.artifacts[0].payload
    finally:
        seed.tearDown()


def test_worker_close_fences_returned_artifact_and_restart_recovers():
    from tests.test_v3_durable_processing import V3DurableProcessingTests, _SuccessfulAdapter
    from allday_asr.v3.application import DurableProcessingWorker
    seed = V3DurableProcessingTests()
    seed.setUp()
    try:
        submitted = seed.service.submit(seed._command())
        worker = DurableProcessingWorker(seed.service, _SuccessfulAdapter(), worker_id='before-close')
        for _ in range(4):
            assert worker.run_once()
        original = worker.adapter.execute

        def close_before_return(context, control):
            result = original(context, control)
            worker.close()
            return result

        with patch.object(worker.adapter, 'execute', side_effect=close_before_return):
            assert worker.run_once()
        assert seed.service.get(submitted.job.job_id).job.status == 'failed_retryable'
        with seed.database.read() as c:
            assert c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 0
        assert not worker.run_once()
        seed.service.retry(submitted.job.job_id)
        restarted = DurableProcessingWorker(seed.service, _SuccessfulAdapter(), worker_id='after-close')
        for _ in range(5):
            assert restarted.run_once()
        assert seed.service.get(submitted.job.job_id).job.status == 'succeeded'
        with seed.database.read() as c:
            assert c.execute('SELECT COUNT(*) FROM artifacts').fetchone()[0] == 2
    finally:
        seed.tearDown()


@pytest.mark.skipif(sys.platform != 'win32', reason='Exclusive Windows Job')
@pytest.mark.parametrize('phase', ['model_load', 'audio_prepare', 'transcribe', 'diarize', 'artifact_write', 'backend_close'])
def test_native_hangs_terminate_owned_tree(tmp_path, phase):
    import psutil
    from allday_asr.v3.adapters.models.isolated_native import run_owned_stage
    marker = tmp_path / 'owned.json'
    control = SimpleNamespace(heartbeat=lambda _: False, cancellation_requested=lambda: False)
    with pytest.raises(TimeoutError):
        run_owned_stage(__name__ + ':synthetic_native_call', {'marker': str(marker), 'phase': phase, 'hang': True, 'child': True},
            control, threading.Event(), tmp_path, stage_seconds=5, progress_seconds=2)
    ids = json.loads(marker.read_text())
    # Windows retires terminated process objects asynchronously even after the
    # Job's ActiveProcesses reaches zero. Assert disappearance within a bound.
    deadline = time.monotonic() + 2
    while any(psutil.pid_exists(pid) for pid in ids.values() if pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not psutil.pid_exists(ids['pid']) and not psutil.pid_exists(ids['child'])


@pytest.mark.skipif(sys.platform != 'win32', reason='Exclusive Windows Job')
def test_native_long_progress_and_shutdown_are_bounded(tmp_path):
    from allday_asr.v3.adapters.models.isolated_native import run_owned_stage
    from allday_asr.v3.ports.processing import StageCancellationRequested
    control = SimpleNamespace(heartbeat=lambda _: False, cancellation_requested=lambda: False)
    payload = {'marker': str(tmp_path / 'pid.json'), 'ticks': 20}
    assert run_owned_stage(__name__ + ':synthetic_native_call', payload, control, threading.Event(), tmp_path,
        stage_seconds=6, progress_seconds=2) == {'checkpoint': 'finished'}
    stopped = threading.Event()
    timer = threading.Timer(1, stopped.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(StageCancellationRequested):
            run_owned_stage(__name__ + ':synthetic_native_call', {**payload, 'hang': True}, control, stopped, tmp_path,
                stage_seconds=6, progress_seconds=3)
        assert time.monotonic() - started < 3
    finally:
        timer.cancel()
