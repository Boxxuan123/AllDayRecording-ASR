"""Transaction boundaries and work counts on real isolated SQLite databases."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from tests.test_phase1_human_facts import people as people
from tests.annotation_sync_fixture import seed
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain.device_sync import ClientOperation, SyncRequest, PROJECTION_VERSION
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.interfaces.device_annotations import DeviceAnnotationService
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService


def test_read_endpoints_do_not_wait_for_writer(people):
    f = people
    sid, _, ids = seed(f, 2)
    queries = [lambda: f.core.desktop.session_detail(sid),
               lambda: f.core.desktop.list_reviews(500), f.core.desktop.list_devices,
               f.core.people.list_people, f.core.people.list_review_candidates,
               lambda: f.core.corrections.correction_history(ids[0])]
    # Hold SQLite's only writer open while another connection reads each endpoint.
    with f.core.database.transaction(), ThreadPoolExecutor(1) as pool:
        for query in queries:
            pool.submit(query).result(timeout=2)


def test_batch_reuses_person_and_coalesces_session_queue(people):
    f = people
    sid, pid, ids = seed(f, 32)
    with f.core.database.transaction() as db:
        db.execute("UPDATE annotation_sample_queue SET status='running',generation=10,token='old',lease_until=9999999999")
        db.execute("CREATE TABLE queue_writes(n INTEGER)")
        db.execute("CREATE TRIGGER count_queue_updates AFTER UPDATE ON annotation_sample_queue BEGIN INSERT INTO queue_writes VALUES(1); END")
    statements = []
    connect = f.core.database._connect
    def traced():
        connection = connect()
        connection.set_trace_callback(statements.append)
        return connection
    ops = tuple(ClientOperation(new_ulid(), 'speaker.assign', None,
        {'person_id':pid,'selections':[{'utterance_id':uid,'revision':1}]}) for uid in ids)
    request = SyncRequest(PROJECTION_VERSION, None, ops, 500)
    with patch.object(f.core.database, '_connect', side_effect=traced):
        result = f.core.mobile_sync.synchronize('device-1', request)
    assert all(r.status.value == 'applied' for r in result.receipts)
    assert len({r.operation_id for r in result.receipts}) == 32
    assert sum(s.startswith('SELECT person_id,display_name,kind FROM persons WHERE') for s in statements) == 1
    with f.core.database.read() as db:
        assert db.execute('SELECT count(*) FROM queue_writes').fetchone()[0] == 1
        assert db.execute('SELECT generation FROM annotation_sample_queue WHERE session_id=?',(sid,)).fetchone()[0] == 11
    assert f.core.mobile_sync.synchronize('device-1', request).receipts == result.receipts
    # A different operation with an old revision remains a conflict, not a merged success.
    conflict = ClientOperation(new_ulid(), 'speaker.assign', None, ops[0].payload)
    assert f.core.mobile_sync.synchronize('device-1', SyncRequest(PROJECTION_VERSION,None,(conflict,),500)).receipts[0].status.value == 'conflict'
    with SqliteUnitOfWork(f.core.database) as uow:
        uow.people.finish_sample_job(sid,10,'old','processed','',1)
        assert uow.people.connection.execute('SELECT status FROM annotation_sample_queue').fetchone()[0] == 'queued'


def test_snapshot_versions_track_contents(people):
    service = DeviceAnnotationService(people.core)
    reviews = DeviceReviewService(people.core)
    first = service.execute('device-1',{'action':'people'})
    assert first == service.execute('device-1',{'action':'people'})
    initial_reviews = reviews.snapshot()
    assert initial_reviews['version'] == reviews.snapshot()['version']
    people.core.people.create_person('Synthetic new person')
    assert first['version'] != service.execute('device-1',{'action':'people'})['version']


def test_rematch_computes_without_writer_and_rejects_stale_inputs(people):
    from allday_asr.v3.application.people_identity import layered_person_match
    f = people
    f._seed_track(2)
    from tests.test_v34_open_speaker_identity import _session_id
    f.core.people.analyze(_session_id(2))
    with ThreadPoolExecutor(1) as pool:
        def changed(*args, **kwargs):
            # Completes only if the match calculation does not hold a writer.
            pool.submit(f.core.people.create_person, 'Concurrent person').result(timeout=2)
            return layered_person_match(*args, **kwargs)
        with patch('allday_asr.v3.application.people_identity.layered_person_match',side_effect=changed):
            with pytest.raises(ValueError,match='inputs changed'):
                f.core.people.rematch_existing(_session_id(2))
