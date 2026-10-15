"""PRODUCT_REMINDER_E2E_TEST: synthetic, isolated SQLite; no speaker learning."""

from allday_asr.v3 import PROJECTION_VERSION
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import pytest

from tests import test_v33_intelligent_reminders as seed
from allday_asr.v3.application import ReminderExtractionService, MobileSyncService
from allday_asr.v3.application.mobile_sync import UtteranceCorrectionOperationHandler
from allday_asr.v3.domain import ClientOperation, SyncRequest, new_ulid
from allday_asr.v3.domain.reminder_time import explicit_task


@pytest.fixture
def env():
    instance = seed.V33IntelligentReminderTests()
    instance.setUp()
    instance.current_time = datetime(2026, 10, 2, 1, tzinfo=timezone.utc)
    with instance.database.transaction() as c:
        c.execute(
            "UPDATE utterances SET text=?,start_at=?,end_at=? WHERE utterance_id=?",
            (
                "我明天下午三点给老师发材料",
                "2026-10-01T13:00:00Z",
                "2026-10-01T13:00:01Z",
                seed.UTTERANCE_ID,
            ),
        )
    instance.extraction = ReminderExtractionService(
        instance.factory, instance.reminders, None, now=lambda: instance.current_time
    )
    instance.sync = MobileSyncService(
        instance.factory,
        operation_handler=UtteranceCorrectionOperationHandler(
            now=lambda: instance.current_time
        ),
        now=lambda: instance.current_time,
    )
    yield instance
    instance.tearDown()


def candidate(env):
    return env.extraction.extract_product(seed.SESSION_ID)["candidates"][0]


def operation(env, kind, payload, revision=None, operation_id=None):
    command = ClientOperation(operation_id or new_ulid(), kind, revision, payload)
    response = env.sync.synchronize(
        "computer-1", SyncRequest(PROJECTION_VERSION, "cursor-0", (command,), 100)
    )
    return command, response


def confirmed(env):
    item = candidate(env)
    command, response = operation(
        env,
        "reminder.review",
        {"candidate_id": item["candidate_id"], "action": "confirm"},
    )
    assert response.receipts[0].status == "applied"
    return env.reminders.list_schedules()[0], command, response


def test_recording_anchor_and_provenance(env):
    item = candidate(env)
    assert item["scheduled_at"] == "2026-10-02T07:00:00.000000Z"
    assert item["status"] == "pending_confirmation"
    assert env.reminders.list_schedules() == ()
    assert item["input_scope"]["recorded_at"] == "2026-10-01T13:00:00Z"
    assert item["input_scope"]["time_expression"] == "明天下午三点"
    assert item["input_scope"]["timezone"] == "Asia/Singapore"
    assert item["input_scope"]["source_text"] == "我明天下午三点给老师发材料"
    assert item["evidence"][0]["utterance_id"] == seed.UTTERANCE_ID


def test_same_source_three_runs_and_concurrent_retry_single_candidate(env):
    with ThreadPoolExecutor(max_workers=3) as pool:
        values = list(pool.map(lambda _: candidate(env)["candidate_id"], range(3)))
    assert len(set(values)) == 1
    assert len(env.reminders.list_candidates()) == 1


def test_new_candidate_review_has_no_existing_source_revision(env):
    item = candidate(env)
    assert item["expected_revision"] == 0
    with env.factory().reading() as uow:
        review = next(
            row
            for row in uow.desktop.list_reviews(50)
            if row["source_id"] == item["candidate_id"]
        )
    assert review["source_revision"] is None
    assert review["context"]["source_text"] == "我明天下午三点给老师发材料"
    assert review["context"]["scheduled_at"] == item["scheduled_at"]


@pytest.mark.parametrize(
    "text",
    [
        "我昨天已经发过了",
        "不用发了",
        "这个取消",
        "我本来准备明天发，但是现在不用了",
        "不用提醒我发材料了",
        "我明天下午三点不用发材料",
        "明天下午三点发材料",
        "我明天三点发材料",
        "我每天下午三点发材料",
        "我明天下午三点发材料吗？",
        "我明天下午三点不发材料",
        "我明天下午三点老师发材料",
    ],
)
def test_negative_completed_recurring_or_unclear_actor_cannot_create(env, text):
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET text=?", (text,))
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []
    assert env.reminders.list_schedules() == ()


def test_unknown_speaker_not_silently_self(env):
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET identity='unknown'")
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []


def test_confirmation_sync_restart_and_http_retry(env):
    schedule, command, response = confirmed(env)
    replay = env.sync.synchronize(
        "computer-1", SyncRequest(PROJECTION_VERSION, "cursor-0", (command,), 100)
    )
    assert replay.receipts == response.receipts
    assert len(env.reminders.list_schedules()) == 1
    assert len(env.reminders.list_candidates()) == 1
    assert any(change.resource_type == "reminder" for change in response.changes)
    from allday_asr.v3.adapters.sqlite import V3Database, SqliteUnitOfWork

    with SqliteUnitOfWork(V3Database(env.database.path)).reading() as uow:
        assert uow.reminders.get_schedule(schedule["event_id"]).status == "scheduled"
    assert candidate(env)["candidate_id"] == schedule["source_candidate_id"]


def test_candidate_edit_ignore_and_no_reappearance(env):
    item = candidate(env)
    _, response = operation(
        env,
        "reminder.review",
        {
            "candidate_id": item["candidate_id"],
            "action": "edit",
            "scheduled_at": "2026-10-02T08:00:00+00:00",
            "title": "给老师发最终材料",
        },
    )
    assert response.receipts[0].status == "applied"
    schedule = env.reminders.list_schedules()[0]
    assert schedule["scheduled_at"] == "2026-10-02T08:00:00.000000Z"
    assert candidate(env)["status"] == "modified"


def test_ignore_survives_reprocessing_and_restart(env):
    item = candidate(env)
    _, response = operation(
        env,
        "reminder.review",
        {"candidate_id": item["candidate_id"], "action": "ignore"},
    )
    assert response.receipts[0].status == "applied"
    assert candidate(env)["status"] == "ignored"
    assert len(env.reminders.list_candidates()) == 1
    assert env.reminders.list_schedules() == ()


def test_old_source_replay_beyond_recent_candidate_window(env):
    from dataclasses import replace

    original_id = candidate(env)["candidate_id"]
    with env.factory() as uow:
        original = uow.reminders.get_candidate(original_id)
        proposal = uow.knowledge.get_proposal(original.proposal_id)
        for index in range(501):
            newer = replace(proposal, proposal_id=new_ulid())
            uow.knowledge.add_proposal(newer)
            uow.reminders.add_candidate(
                replace(
                    original,
                    candidate_id=new_ulid(),
                    proposal_id=newer.proposal_id,
                    created_at=original.created_at + timedelta(seconds=index + 1),
                )
            )
    assert original_id not in {
        item["candidate_id"] for item in env.reminders.list_candidates(limit=500)
    }
    assert candidate(env)["candidate_id"] == original_id


@pytest.mark.parametrize(
    "terminal,status", [("cancel", "cancelled"), ("complete", "completed")]
)
def test_reschedule_terminal_and_stale_revision(env, terminal, status):
    schedule, _, _ = confirmed(env)
    eid = schedule["event_id"]
    _, response = operation(
        env,
        "reminder.task",
        {
            "event_id": eid,
            "action": "reschedule",
            "scheduled_at": "2026-10-02T08:00:00Z",
        },
        1,
    )
    assert response.receipts[0].status == "applied"
    assert env.reminders.schedule(eid)["event_revision"] == 2
    assert env.reminders.schedule(eid)["scheduled_at"] == "2026-10-02T08:00:00.000000Z"
    _, stale = operation(env, "reminder.task", {"event_id": eid, "action": terminal}, 1)
    assert stale.receipts[0].status == "conflict"
    _, response = operation(
        env, "reminder.task", {"event_id": eid, "action": terminal}, 2
    )
    assert response.receipts[0].status == "applied"
    assert env.reminders.schedule(eid)["status"] == status
    assert len(env.reminders.list_schedules()) == 1
    assert candidate(env)["status"] == "confirmed"


def test_stale_source_identity_due_rejected_before_mutation(env):
    item = candidate(env)
    with env.database.transaction() as c:
        c.execute("UPDATE utterances SET identity='unknown'")
    _, response = operation(
        env,
        "reminder.review",
        {"candidate_id": item["candidate_id"], "action": "confirm"},
    )
    assert response.receipts[0].status == "conflict"
    assert env.reminders.list_schedules() == ()


def test_event_changed_outside_reminder_rejects_task_with_durable_conflict(env):
    from dataclasses import replace

    schedule, _, _ = confirmed(env)
    with env.factory() as uow:
        state = uow.knowledge.get_event(schedule["event_id"])
        uow.knowledge.put_event_state(
            replace(state, revision=state.revision + 1), state.revision
        )
    command, response = operation(
        env,
        "reminder.task",
        {"event_id": schedule["event_id"], "action": "complete"},
        1,
    )
    assert response.receipts[0].status == "conflict"
    replay = env.sync.synchronize(
        "computer-1", SyncRequest(PROJECTION_VERSION, "cursor-0", (command,), 100)
    )
    assert replay.receipts == response.receipts
    assert env.reminders.schedule(schedule["event_id"])["status"] == "scheduled"


def test_unexpected_storage_failure_rolls_back_task_and_receipt(env, monkeypatch):
    item = candidate(env)
    from allday_asr.v3.application.reminder_apply import ReminderApplyMixin

    def fail(*args, **kwargs):
        raise RuntimeError("disk failure")

    monkeypatch.setattr(ReminderApplyMixin, "_apply_candidate", fail)
    command = ClientOperation(
        new_ulid(),
        "reminder.review",
        None,
        {"candidate_id": item["candidate_id"], "action": "confirm"},
    )
    with pytest.raises(RuntimeError, match="disk failure"):
        env.sync.synchronize(
            "computer-1", SyncRequest(PROJECTION_VERSION, "cursor-0", (command,), 100)
        )
    assert env.knowledge.list_events(seed.SESSION_ID) == ()
    assert (
        env.reminders.candidate(item["candidate_id"])["status"]
        == "pending_confirmation"
    )
    with env.factory().reading() as uow:
        assert uow.mobile_sync.find_operation(command.operation_id) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我今晚9点20提醒我检查测试结果", "2026-10-01T21:20:00+08:00"),
        ("我后天上午十点半发材料", "2026-10-03T10:30:00+08:00"),
        ("我2026年10月4日下午四点发材料", "2026-10-04T16:00:00+08:00"),
    ],
)
def test_supported_explicit_time_forms(text, expected):
    result = explicit_task(text, "2026-10-01T13:00:00Z", "Asia/Singapore")
    assert result[1].isoformat() == expected


def test_overdue_delayed_sync_does_not_roll_forward(env):
    env.current_time += timedelta(days=2)
    assert env.extraction.extract_product(seed.SESSION_ID)["candidates"] == []


def add_second_product_source(env):
    with env.database.transaction() as c:
        row = dict(c.execute('SELECT * FROM utterances WHERE utterance_id=?', (seed.UTTERANCE_ID,)).fetchone())
        row.update(utterance_id=new_ulid(), ordinal=1, text='我明天下午四点给同学发照片',
                   original_text='我明天下午四点给同学发照片',
                   start_at='2026-10-01T13:01:00Z', end_at='2026-10-01T13:01:01Z')
        c.execute(f'INSERT INTO utterances ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    return row['utterance_id']


@pytest.mark.parametrize('entry', ['desktop', 'phone'])
def test_fup01_independent_product_candidates_both_confirm_through_sync(env, entry):
    add_second_product_source(env)
    items = env.extraction.extract_product(seed.SESSION_ID)['candidates']
    assert len(items) == 2
    for item in items:
        if entry == 'desktop':
            env.reminders.confirm(item['candidate_id'], 'desktop-user')
        else:
            _, response = operation(env, 'reminder.review', {'candidate_id': item['candidate_id'], 'action': 'confirm'})
            assert response.receipts[0].status == 'applied'
        assert env.reminders.candidate(item['candidate_id'])['status'] == 'confirmed'
    assert len(env.reminders.list_schedules()) == 2
    assert len({s['scheduled_at'] for s in env.reminders.list_schedules()}) == 2


@pytest.mark.parametrize('ordering', ['reverse', 'restart', 'concurrent'])
def test_fup01_independent_confirmation_order_and_restart(env, ordering):
    add_second_product_source(env)
    items = env.extraction.extract_product(seed.SESSION_ID)['candidates']
    if ordering == 'concurrent':
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda item: env.reminders.confirm(item['candidate_id'], 'user'), items))
    else:
        items = items[::-1] if ordering == 'reverse' else items
        env.reminders.confirm(items[0]['candidate_id'], 'user')
        if ordering == 'restart':
            from allday_asr.v3.application.reminders import IntelligentReminderService
            from allday_asr.v3.application.knowledge import KnowledgeArchitectureService
            env.reminders = IntelligentReminderService(env.factory, KnowledgeArchitectureService(env.factory), now=lambda: env.current_time)
        env.reminders.confirm(items[1]['candidate_id'], 'user')
    assert len(env.reminders.list_schedules()) == 2
    assert all(env.reminders.candidate(i['candidate_id'])['status'] == 'confirmed' for i in items)


@pytest.mark.parametrize('mutation', ['text', 'revision', 'identity', 'unavailable', 'session_unavailable', 'timezone', 'person_review'])
@pytest.mark.parametrize('entry', ['desktop', 'phone'])
def test_fup01_actual_rule_source_changes_cannot_publish(env, mutation, entry):
    item = candidate(env)
    with env.database.transaction() as c:
        if mutation == 'timezone':
            c.execute("UPDATE recording_sessions SET timezone='UTC'")
        elif mutation == 'session_unavailable':
            c.execute("UPDATE recording_sessions SET tombstoned_at='2026-10-02T00:00:00Z'")
        elif mutation == 'person_review':
            c.execute("UPDATE utterances SET evidence_json=json_set(evidence_json,'$.annotation_review',json(?))",
                      ('{"dimensions":{"person":{"reason":"uncertain"}}}',))
        else:
            assignment = {'text': "text='我明天下午五点发材料'", 'revision': 'revision=revision+1',
                          'identity': "identity='unknown'", 'unavailable': "status='stale'"}[mutation]
            c.execute('UPDATE utterances SET ' + assignment)
    if entry == 'desktop':
        assert env.reminders.confirm(item['candidate_id'], 'user')['status'] == 'conflict'
    else:
        _, response = operation(env, 'reminder.review', {'candidate_id': item['candidate_id'], 'action': 'confirm'})
        assert response.receipts[0].status == 'conflict'
    assert env.reminders.list_schedules() == ()


@pytest.mark.parametrize('version,kind', [(99, 'product-reminder'), (1, 'product-reminder'), (2, 'reminder')])
@pytest.mark.parametrize('entry', ['desktop', 'phone'])
def test_fup01_unknown_snapshot_format_cannot_publish(env, version, kind, entry):
    import json

    item = candidate(env)
    scope = dict(item['input_scope'])
    scope['input_snapshot'] = {**scope['input_snapshot'], 'version': version, 'kind': kind}
    with env.database.transaction() as c:
        c.execute('UPDATE generation_records SET input_scope_json=? WHERE generation_id=?',
                  (json.dumps(scope), item['generation_id']))
    if entry == 'desktop':
        assert env.reminders.confirm(item['candidate_id'], 'user')['status'] == 'conflict'
    else:
        _, response = operation(env, 'reminder.review', {'candidate_id': item['candidate_id'], 'action': 'confirm'})
        assert response.receipts[0].status == 'conflict'
    assert env.reminders.list_schedules() == ()


def test_fup01_unused_source_change_and_single_candidate_replay(env):
    other = add_second_product_source(env)
    first = candidate(env)
    with env.database.transaction() as c:
        c.execute('UPDATE utterances SET text=?,revision=revision+1 WHERE utterance_id=?', ('无关的话', other))
    command, response = operation(env, 'reminder.review', {'candidate_id': first['candidate_id'], 'action': 'confirm'})
    assert response.receipts[0].status == 'applied'
    replay = env.sync.synchronize('computer-1', SyncRequest(PROJECTION_VERSION, 'cursor-0', (command,), 100))
    assert replay.receipts == response.receipts
    with ThreadPoolExecutor(2) as pool:
        def repeat(_):
            with pytest.raises(ValueError):
                env.reminders.confirm(first['candidate_id'], 'user')
        list(pool.map(repeat, range(2)))
    assert len(env.reminders.list_schedules()) == 1


def legacy_product_snapshot(env, item, *, trusted=True):
    import json
    from allday_asr.v3.application.generation_inputs import input_snapshot
    scope = dict(item['input_scope'])
    scope['input_snapshot'] = input_snapshot('reminder', env.extraction._request(seed.SESSION_ID)) if trusted else {'version': 99}
    with env.database.transaction() as c:
        c.execute('UPDATE generation_records SET input_scope_json=? WHERE generation_id=?',
                  (json.dumps(scope), item['generation_id']))


@pytest.mark.parametrize('original_status', ['pending', 'conflict', 'untrusted', 'ignored', 'source_changed'])
def test_fup01_legacy_explicit_recovery_preserves_history_and_is_idempotent(env, original_status):
    add_second_product_source(env)
    items = env.extraction.extract_product(seed.SESSION_ID)['candidates']
    old = items[1]
    legacy_product_snapshot(env, old, trusted=original_status != 'untrusted')
    if original_status == 'conflict':
        env.reminders.confirm(items[0]['candidate_id'], 'user')
        assert env.reminders.confirm(old['candidate_id'], 'user')['status'] == 'conflict'
    elif original_status == 'ignored':
        env.reminders.ignore(old['candidate_id'], 'user', 'no longer wanted')
    elif original_status == 'source_changed':
        with env.database.transaction() as c:
            c.execute('UPDATE utterances SET revision=revision+1')
    report = env.extraction.inspect_product_recovery(old['candidate_id'])
    if original_status not in {'pending', 'conflict'}:
        assert not report['eligible']
        with pytest.raises(ValueError, match='source review'):
            env.extraction.recover_product(old['candidate_id'], 'user')
        return
    assert report['eligible']
    recovered = env.extraction.recover_product(old['candidate_id'], 'user')['candidate']
    assert recovered['status'] == 'pending_confirmation'
    assert recovered['candidate_id'] != old['candidate_id']
    assert recovered['input_scope']['recovery_of_generation_id'] == old['generation_id']
    assert env.extraction.recover_product(old['candidate_id'], 'user')['candidate']['candidate_id'] == recovered['candidate_id']
    env.reminders.confirm(recovered['candidate_id'], 'user')
    assert env.reminders.candidate(old['candidate_id'])['status'] == 'conflict'
    assert recovered['candidate_id'] in {v['candidate_id'] for v in env.extraction.extract_product(seed.SESSION_ID)['candidates']}
    with env.database.read() as c:
        assert c.execute("SELECT count(*) FROM audit_entries WHERE action='reminder.product.recovered'").fetchone()[0] == 1


class BatchReminderGenerator:
    model_label = producer_version = prompt_version = extractor_version = 'synthetic-batch'

    def generate(self, request, effort):
        from allday_asr.v3.ports.reminder_generation import ReminderModelResult
        values = tuple({'operation': 'CREATE_TASK', 'title': f'Batch task {i}', 'actor_person_id': 'self',
                        'commitment_direction': 'not_applicable', 'related_person_ids': [],
                        'scheduled_at': f'2026-10-02T0{7+i}:00:00Z', 'location': None, 'confidence': 1.0,
                        'evidence_utterance_ids': [r['utterance_id']], 'needs_confirmation': True,
                        'target_event_id': None, 'expected_revision': 0, 'reason': None}
                       for i, r in enumerate(request.utterances))
        return ReminderModelResult(values, 'synthetic', effort)

    def close(self):
        pass


def model_batch(env):
    add_second_product_source(env)
    extraction = ReminderExtractionService(env.factory, env.reminders, BatchReminderGenerator(), now=lambda: env.current_time)
    return extraction.extract(seed.SESSION_ID)['candidates']


def test_fup01b_model_batch_modify_after_sibling_confirm(env):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    edited = env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Edited second task'})
    assert edited['candidate']['status'] == 'modified'
    assert edited['replacement']['status'] == 'confirmed'
    assert edited['replacement']['generation_id'] != items[1]['generation_id']
    assert len(env.reminders.list_schedules()) == 2
    assert edited['reminder']['title'] == 'Edited second task'


@pytest.mark.parametrize('external_change', [False, True])
def test_fup01_model_batch_outputs_require_persisted_batch_proof(env, external_change):
    add_second_product_source(env)
    extraction = ReminderExtractionService(env.factory, env.reminders, BatchReminderGenerator(), now=lambda: env.current_time)
    items = extraction.extract(seed.SESSION_ID)['candidates']
    assert len(items) == 2
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    if external_change:
        schedule = env.reminders.list_schedules()[0]
        _, response = operation(env, 'reminder.task', {'event_id': schedule['event_id'], 'action': 'reschedule',
            'scheduled_at': '2026-10-02T10:00:00Z'}, schedule['event_revision'])
        assert response.receipts[0].status == 'applied'
    env.reminders.confirm(items[1]['candidate_id'], 'user')
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == ('conflict' if external_change else 'confirmed')
    assert len(env.reminders.list_schedules()) == (1 if external_change else 2)


def test_fup01_same_model_target_conflict_keeps_revision_cas(env):
    from dataclasses import replace
    from allday_asr.v3.application.generation_inputs import input_snapshot
    from allday_asr.v3.domain.reminders import ReminderOperation
    schedule, _, _ = confirmed(env)
    request = env.extraction._request(seed.SESSION_ID)
    original = replace(seed._intent(), title='第一次修改', scheduled_at=env.current_time + timedelta(hours=10),
                       target_event_id=schedule['event_id'], expected_revision=1,
                       operation=ReminderOperation.UPDATE_EVENT, needs_confirmation=True)
    submission = replace(seed._submission(original), intents=(original, replace(original, title='第二次修改')),
        producer='codex-reminder-generator', input_scope={'session_id': seed.SESSION_ID, 'input_snapshot': input_snapshot('reminder', request)})
    items = env.reminders.submit_generation(submission, allow_auto_apply=False)['candidates']
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    assert env.reminders.confirm(items[1]['candidate_id'], 'user')['status'] == 'conflict'
    assert env.reminders.schedule(schedule['event_id'])['event_revision'] == 2
    assert env.reminders.schedule(schedule['event_id'])['title'] == '第一次修改'


def test_fup01_modify_independent_candidate_uses_same_rule_dispatcher(env):
    add_second_product_source(env)
    items = env.extraction.extract_product(seed.SESSION_ID)['candidates']
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    edited = env.reminders.modify(items[1]['candidate_id'], 'user', {'title': '修改后的照片任务'})
    assert edited['candidate']['status'] == 'modified'
    assert len(env.reminders.list_schedules()) == 2
    assert len(env.extraction.extract_product(seed.SESSION_ID)['candidates']) == 2


def restart_reminders(env):
    from allday_asr.v3.application import IntelligentReminderService, KnowledgeArchitectureService
    env.database = seed.V3Database.open(env.root / 'core.sqlite3')
    env.knowledge = KnowledgeArchitectureService(env.factory, now=lambda: env.current_time)
    env.reminders = IntelligentReminderService(env.factory, env.knowledge, now=lambda: env.current_time)


def publication_counts(env):
    with env.database.read() as c:
        return (c.execute('SELECT count(*) FROM event_operations').fetchone()[0],
                c.execute("SELECT count(*) FROM change_events WHERE resource_type='reminder'").fetchone()[0])


def correct_batch_source(env):
    _, response = operation(env, 'utterance.correct', {'utterance_id': seed.UTTERANCE_ID,
        'text': '真实修订后的合成材料', 'speaker_track_id': seed.SPEAKER_ID, 'identity': 'self'}, 1)
    assert response.receipts[0].status == 'applied'


@pytest.mark.parametrize('first,restart', [(0, False), (1, False), (0, True), (1, True)])
def test_fup01b_modify_order_restart_and_valid_fields(env, first, restart):
    items = model_batch(env)
    env.reminders.confirm(items[first]['candidate_id'], 'user')
    if restart:
        restart_reminders(env)
    result = env.reminders.modify(items[1-first]['candidate_id'], 'user',
        {'title': 'Legally edited task', 'location': 'Synthetic room', 'scheduled_at': '2026-10-02T12:00:00Z'})
    assert result['candidate']['status'] == 'modified'
    assert result['replacement']['status'] == 'confirmed'
    assert result['reminder']['location'] == 'Synthetic room'
    assert result['reminder']['scheduled_at'].startswith('2026-10-02T12:00:00')
    assert publication_counts(env) == (2, 2)


@pytest.mark.parametrize('changes', [{'title': ''}, {'title': 123}, {'location': 123},
    {'scheduled_at': '2026-10-01T12:00:00Z'}, {'scheduled_at': '2026-10-03T12:00:00'}, {'unknown': 'x'}])
def test_fup01b_invalid_edit_fields_do_not_create_attempt(env, changes):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    with pytest.raises(ValueError):
        env.reminders.modify(items[1]['candidate_id'], 'user', changes)
    assert len(env.reminders.list_candidates()) == 2
    assert publication_counts(env) == (1, 1)


@pytest.mark.parametrize('entry', ['desktop', 'phone'])
@pytest.mark.parametrize('mutation', ['reschedule', 'cancel', 'complete', 'source', 'identity', 'session', 'external_task'])
def test_fup01b_actual_context_change_rejects_edit(env, mutation, entry):
    from dataclasses import replace
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    schedule = env.reminders.list_schedules()[0]
    if mutation in {'reschedule', 'cancel', 'complete'}:
        payload = {'event_id': schedule['event_id'], 'action': mutation}
        if mutation == 'reschedule':
            payload['scheduled_at'] = '2026-10-02T10:00:00Z'
        _, response = operation(env, 'reminder.task', payload, schedule['event_revision'])
        assert response.receipts[0].status == 'applied'
    elif mutation == 'source':
        correct_batch_source(env)
    elif mutation == 'external_task':
        extra = env.reminders.submit_generation(seed._submission(replace(seed._intent(),
            title='External unrelated task', scheduled_at=env.current_time+timedelta(hours=20))), allow_auto_apply=False)['candidates'][0]
        env.reminders.confirm(extra['candidate_id'], 'other-user')
    else:
        with env.database.transaction() as c:
            if mutation == 'identity':
                c.execute("UPDATE utterances SET identity='unknown'")
            else:
                c.execute("UPDATE recording_sessions SET tombstoned_at='2026-10-02T00:00:00Z'")
    counts = publication_counts(env)
    if entry == 'desktop':
        with pytest.raises(ValueError):
            env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Must not publish'})
    else:
        _, response = operation(env, 'reminder.review', {'candidate_id': items[1]['candidate_id'], 'action': 'edit', 'title': 'Must not publish'})
        assert response.receipts[0].status == 'conflict'
    assert publication_counts(env) == counts
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] != 'modified'
    assert env.reminders.schedule(schedule['event_id'])['status'] == ({'cancel': 'cancelled', 'complete': 'completed'}.get(mutation, 'scheduled'))


@pytest.mark.parametrize('window', ['before_submit', 'after_submit'])
@pytest.mark.parametrize('mutation', ['source', 'original_confirm'])
def test_fup01b_transaction_gap_rejects_stale_replacement(env, monkeypatch, window, mutation):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    submit = env.reminders.submit_generation
    counts = []
    def change():
        if mutation == 'source':
            correct_batch_source(env)
        else:
            env.reminders.confirm(items[1]['candidate_id'], 'other-user')
        counts.append(publication_counts(env))
    def interleaved(submission, **kwargs):
        if window == 'before_submit':
            change()
        result = submit(submission, **kwargs)
        if window == 'after_submit':
            change()
        return result
    monkeypatch.setattr(env.reminders, 'submit_generation', interleaved)
    with pytest.raises(ValueError):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Stale replacement'})
    assert publication_counts(env) == counts[0]
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] != 'modified'
    assert all(v['title'] != 'Stale replacement' for v in env.reminders.list_schedules())


def test_fup01b_publish_failure_restart_reuses_unaccepted_attempt(env, monkeypatch):
    from allday_asr.v3.adapters.sqlite.reminder_repository import SqliteReminderRepository
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    feedback = SqliteReminderRepository.add_feedback
    def fail_edit_feedback(self, value):
        if value.action.value == 'modify':
            raise RuntimeError('synthetic storage failure after modified write')
        return feedback(self, value)
    monkeypatch.setattr(SqliteReminderRepository, 'add_feedback', fail_edit_feedback)
    with pytest.raises(RuntimeError, match='synthetic storage failure'):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Retry same edit'})
    assert publication_counts(env) == (1, 1)
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'pending_confirmation'
    assert len(env.reminders.list_candidates()) == 3
    with env.database.read() as c:
        assert c.execute('SELECT count(*) FROM generation_records').fetchone()[0] == 2
    monkeypatch.setattr(SqliteReminderRepository, 'add_feedback', feedback)
    restart_reminders(env)
    result = env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Retry same edit'})
    assert result['candidate']['status'] == 'modified'
    assert publication_counts(env) == (2, 2)
    assert len(env.reminders.list_candidates()) == 3
    with pytest.raises(ValueError, match='no longer pending'):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Retry same edit'})
    assert publication_counts(env) == (2, 2)


@pytest.mark.parametrize('second_action', ['confirm', 'same_edit', 'different_edit'])
def test_fup01b_concurrent_decisions_publish_at_most_once(env, monkeypatch, second_action):
    from threading import Barrier
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    accept = env.knowledge.accept_proposal
    barrier = Barrier(2)
    def synchronized(*args, **kwargs):
        barrier.wait(timeout=10)
        return accept(*args, **kwargs)
    monkeypatch.setattr(env.knowledge, 'accept_proposal', synchronized)
    def decide(action):
        try:
            if action == 'confirm':
                return env.reminders.confirm(items[1]['candidate_id'], 'user')
            return env.reminders.modify(items[1]['candidate_id'], 'user',
                {'title': 'Concurrent edit' if action == 'same_edit' else 'Other concurrent edit'})
        except ValueError:
            return None
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(decide, 'same_edit'), pool.submit(decide, second_action)]
        results = [f.result(timeout=20) for f in futures]
    assert sum(value is not None for value in results) == 1
    assert publication_counts(env) == (2, 2)
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] in {'confirmed', 'modified'}
    if second_action == 'same_edit':
        assert len(env.reminders.list_candidates()) == 3
    else:
        for item in env.reminders.list_candidates():
            if item['status'] == 'pending_confirmation':
                monkeypatch.setattr(env.knowledge, 'accept_proposal', accept)
                env.reminders.confirm(item['candidate_id'], 'user')
    assert publication_counts(env) == (2, 2)


def foreign_pending_candidate(env):
    from dataclasses import replace
    with env.database.transaction() as c:
        session = dict(c.execute('SELECT * FROM recording_sessions LIMIT 1').fetchone())
        session['session_id'] = new_ulid()
        c.execute(f'INSERT INTO recording_sessions ({",".join(session)}) VALUES ({",".join("?" for _ in session)})', tuple(session.values()))
        row = dict(c.execute('SELECT * FROM utterances WHERE utterance_id=?', (seed.UTTERANCE_ID,)).fetchone())
        run = dict(c.execute('SELECT * FROM processing_runs WHERE run_id=?', (row['run_id'],)).fetchone())
        run.update(run_id=new_ulid(), session_id=session['session_id'])
        c.execute(f'INSERT INTO processing_runs ({",".join(run)}) VALUES ({",".join("?" for _ in run)})', tuple(run.values()))
        artifact = dict(c.execute('SELECT * FROM artifacts WHERE artifact_id=?', (row['source_artifact_id'],)).fetchone())
        artifact.update(artifact_id=new_ulid(), run_id=run['run_id'])
        c.execute(f'INSERT INTO artifacts ({",".join(artifact)}) VALUES ({",".join("?" for _ in artifact)})', tuple(artifact.values()))
        row.update(utterance_id=new_ulid(), session_id=session['session_id'], speaker_track_id=None,
                   original_speaker_track_id=None, run_id=run['run_id'], source_artifact_id=artifact['artifact_id'])
        c.execute(f'INSERT INTO utterances ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    intent = replace(seed._intent(), session_id=session['session_id'], evidence_utterance_ids=(row['utterance_id'],),
                     title='Foreign task', scheduled_at=env.current_time+timedelta(hours=22))
    submission = replace(seed._submission(intent), input_scope={'session_id': session['session_id']})
    return env.reminders.submit_generation(submission, allow_auto_apply=False)['candidates'][0]


@pytest.mark.parametrize('forgery', ['batch_id', 'missing', 'missing_candidate', 'wrong_parent', 'foreign_session',
    'evidence', 'snapshot', 'producer', 'operation', 'cycle', 'mismatched_session_scope'])
def test_fup01b_untrusted_edit_link_cannot_grant_batch_exemption(env, monkeypatch, forgery):
    import copy
    import json
    from dataclasses import replace
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    foreign = foreign_pending_candidate(env) if forgery in {'foreign_session', 'mismatched_session_scope'} else None
    submit = env.knowledge.submit_generation
    attempted = []
    def forged(submission, **kwargs):
        attempted.append(submission)
        scope = copy.deepcopy(submission.input_scope)
        producer = submission.producer
        proposals = submission.proposals
        if forgery == 'batch_id':
            scope['source_generation_id'] = items[0]['generation_id']
        elif forgery == 'missing':
            scope.pop('revises_candidate_id')
        elif forgery == 'missing_candidate':
            scope['revises_candidate_id'] = new_ulid()
        elif forgery == 'wrong_parent':
            scope['revises_candidate_id'] = items[0]['candidate_id']
        elif forgery == 'foreign_session':
            scope['revises_candidate_id'] = foreign['candidate_id']
        elif forgery == 'evidence':
            proposals = ((proposals[0][0], proposals[0][1], (seed.UTTERANCE_ID,)),)
        elif forgery == 'snapshot':
            scope['input_snapshot']['inputs']['active_reminders'] = [env.reminders.list_schedules()[0]]
        elif forgery == 'producer':
            producer = 'codex-reminder-generator'
        elif forgery == 'operation':
            payload = dict(proposals[0][1], operation='update', event_id=env.reminders.list_schedules()[0]['event_id'], expected_revision=1)
            proposals = ((proposals[0][0], payload, proposals[0][2]),)
        elif forgery == 'cycle':
            with env.database.transaction() as c:
                original = dict(items[1]['input_scope'], revises_candidate_id=items[1]['candidate_id'])
                c.execute("UPDATE generation_records SET producer='desktop-user',model='manual-edit',input_scope_json=? WHERE generation_id=?",
                          (json.dumps(original), items[1]['generation_id']))
        else:
            scope['revises_candidate_id'] = foreign['candidate_id']
            scope['session_id'] = foreign['session_id']
            scope['input_snapshot'] = foreign['input_scope']['input_snapshot']
        return submit(replace(submission, input_scope=scope, producer=producer, proposals=proposals), **kwargs)
    monkeypatch.setattr(env.knowledge, 'submit_generation', forged)
    with pytest.raises(ValueError):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Forged edit'})
    assert len(attempted) == 1
    assert publication_counts(env) == (1, 1)
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'pending_confirmation'


@pytest.mark.parametrize('entry', ['desktop', 'phone'])
def test_fup01b_valid_model_edit_has_equivalent_phone_and_desktop_result(env, entry):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    if entry == 'desktop':
        result = env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Shared valid title'})
        replacement_id = result['replacement']['candidate_id']
    else:
        command, response = operation(env, 'reminder.review', {'candidate_id': items[1]['candidate_id'],
            'action': 'edit', 'title': 'Shared valid title'})
        assert response.receipts[0].status == 'applied'
        replay = env.sync.synchronize('computer-1', SyncRequest(PROJECTION_VERSION, 'cursor-0', (command,), 100))
        assert replay.receipts == response.receipts
        replacement_id = next(r for r in env.reminders.list_schedules() if r['title'] == 'Shared valid title')['source_candidate_id']
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'modified'
    replacement = next(c for c in env.reminders.list_candidates() if c['candidate_id'] != items[1]['candidate_id'] and c['title'] == 'Shared valid title')
    assert replacement['candidate_id'] == replacement_id
    assert replacement['status'] == 'confirmed'
    assert replacement['generation_id'] != items[1]['generation_id']
    assert publication_counts(env) == (2, 2)


@pytest.mark.parametrize('corruption', ['missing', 'cycle', 'other_generation', 'session', 'snapshot', 'batch_id'])
def test_fup01b_final_accept_rechecks_persisted_relationship(env, monkeypatch, corruption):
    import copy
    import json
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    foreign = foreign_pending_candidate(env) if corruption == 'other_generation' else None
    submit = env.reminders.submit_generation
    attempted = []
    def corrupt_after_submit(submission, **kwargs):
        attempted.append(submission)
        result = submit(submission, **kwargs)
        replacement = result['candidates'][0]
        scope = copy.deepcopy(replacement['input_scope'])
        if corruption == 'missing':
            scope.pop('revises_candidate_id')
        elif corruption == 'cycle':
            scope['revises_candidate_id'] = replacement['candidate_id']
        elif corruption == 'session':
            scope['session_id'] = 'other-session'
        elif corruption == 'snapshot':
            scope['input_snapshot']['version'] = 99
        elif corruption == 'batch_id':
            scope['source_generation_id'] = items[0]['generation_id']
        elif corruption == 'other_generation':
            scope.update(revises_candidate_id=foreign['candidate_id'], session_id=foreign['session_id'],
                         input_snapshot=foreign['input_scope']['input_snapshot'])
        with env.database.transaction() as c:
            c.execute('UPDATE generation_records SET input_scope_json=? WHERE generation_id=?',
                      (json.dumps(scope), replacement['generation_id']))
        return result
    monkeypatch.setattr(env.reminders, 'submit_generation', corrupt_after_submit)
    with pytest.raises(ValueError):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Persisted forgery'})
    assert len(attempted) == 1
    assert publication_counts(env) == (1, 1)
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'pending_confirmation'


@pytest.mark.parametrize('trusted', [True, False])
def test_fup01b_legacy_pending_edit_requires_persisted_source_link(env, monkeypatch, trusted):
    import json
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    submit = env.reminders.submit_generation
    replacement = []
    def stop_after_submission(submission, **kwargs):
        result = submit(submission, **kwargs)
        replacement.append(result['candidates'][0])
        raise RuntimeError('synthetic process stopped after submission')
    monkeypatch.setattr(env.reminders, 'submit_generation', stop_after_submission)
    with pytest.raises(RuntimeError, match='process stopped'):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Legacy linked edit'})
    scope = dict(replacement[0]['input_scope'])
    scope.pop('product_source_key')
    if not trusted:
        scope.pop('revises_candidate_id')
    with env.database.transaction() as c:
        c.execute('UPDATE generation_records SET input_scope_json=? WHERE generation_id=?',
                  (json.dumps(scope), replacement[0]['generation_id']))
    restart_reminders(env)
    env.reminders.confirm(replacement[0]['candidate_id'], 'user')
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == ('modified' if trusted else 'pending_confirmation')
    assert env.reminders.candidate(replacement[0]['candidate_id'])['status'] == ('confirmed' if trusted else 'conflict')
    assert publication_counts(env) == ((2, 2) if trusted else (1, 1))
    with env.factory().reading() as uow:
        old = uow.knowledge.get_generation(items[1]['generation_id'])
        new = uow.knowledge.get_generation(replacement[0]['generation_id'])
        assert old.input_scope['input_snapshot'] == new.input_scope['input_snapshot']


def test_fup01b_unchanged_edit_fields_only_supersede_original_candidate(env):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    result = env.reminders.modify(items[1]['candidate_id'], 'user', {'title': items[1]['title']})
    assert result['candidate']['status'] == 'modified'
    assert result['replacement']['status'] == 'confirmed'
    assert publication_counts(env) == (2, 2)


def test_fup01b_sqlite_rejects_candidate_generation_rebinding(env):
    import sqlite3
    items = model_batch(env)
    foreign = foreign_pending_candidate(env)
    with pytest.raises(sqlite3.IntegrityError, match='candidate content is immutable'):
        with env.database.transaction() as c:
            c.execute('UPDATE reminder_candidates SET generation_id=? WHERE candidate_id=?',
                      (foreign['generation_id'], items[1]['candidate_id']))
    assert env.reminders.candidate(items[1]['candidate_id'])['generation_id'] == items[1]['generation_id']
    assert publication_counts(env) == (0, 0)


def test_fup01b_bare_knowledge_accept_cannot_half_publish_edit(env, monkeypatch):
    items = model_batch(env)
    env.reminders.confirm(items[0]['candidate_id'], 'user')
    submit = env.reminders.submit_generation
    replacement = []
    def stop_after_submit(submission, **kwargs):
        result = submit(submission, **kwargs)
        replacement.append(result['candidates'][0])
        raise RuntimeError('synthetic stop before reminder acceptance')
    monkeypatch.setattr(env.reminders, 'submit_generation', stop_after_submit)
    with pytest.raises(RuntimeError, match='synthetic stop'):
        env.reminders.modify(items[1]['candidate_id'], 'user', {'title': 'Only reminder review can publish'})
    with pytest.raises(ValueError, match='reminder review'):
        env.knowledge.accept_proposal(replacement[0]['proposal_id'], 'desktop-user')
    assert publication_counts(env) == (1, 1)
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'pending_confirmation'
    env.reminders.confirm(replacement[0]['candidate_id'], 'user')
    assert env.reminders.candidate(items[1]['candidate_id'])['status'] == 'modified'
    assert publication_counts(env) == (2, 2)
