"""Real core persistence: an applied annotation survives a lost response unchanged."""
from tests import test_v34_open_speaker_identity as seed
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.domain import ClientOperation, SyncRequest, stable_ulid
from allday_asr.v3 import PROJECTION_VERSION


def test_lost_annotation_response_keeps_operation_id_and_applies_once():
    fixture = seed.V34OpenSpeakerIdentityTests()
    fixture.setUp()
    try:
        fixture._seed_track(1)
        operation = ClientOperation(stable_ulid('phase2a-lost-response'), 'segment.classify', None,
            {'selections': [{'utterance_id': seed._utterance_id(1), 'revision': 1}],
             'sound_kind': 'non_speech', 'depends_on': []})
        request = SyncRequest(PROJECTION_VERSION, None, (operation,), 500)
        first = fixture.core.mobile_sync.synchronize('device-1', request)
        assert first.receipts[0].status.value == 'applied'
        with SqliteUnitOfWork(fixture.core.database) as uow:
            revision = uow.evidence.get_utterance(seed._utterance_id(1)).revision
            count = uow.evidence.connection.execute('SELECT count(*) FROM annotation_facts').fetchone()[0]
        # The phone did not receive the first response and resends its durable operation.
        replay = fixture.core.mobile_sync.synchronize('device-1', request)
        assert replay.receipts == first.receipts
        with SqliteUnitOfWork(fixture.core.database) as uow:
            assert uow.evidence.get_utterance(seed._utterance_id(1)).revision == revision
            assert uow.evidence.connection.execute('SELECT count(*) FROM annotation_facts').fetchone()[0] == count
            assert uow.mobile_sync.find_operation(operation.operation_id).operation == operation
    finally:
        fixture.tearDown()
