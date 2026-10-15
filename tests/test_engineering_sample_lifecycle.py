"""Long sampling time is simulated; actual lease and publication paths execute."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_phase1_human_facts import people as people
from tests.test_phase2_samples import audio as audio
from tests.annotation_sync_fixture import seed
from allday_asr.v3.application.annotation_samples import AnnotationSampleWorker
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.adapters.sqlite.annotation_sample_repository import AnnotationSampleRepositoryMixin


def test_long_embedding_renews_lease_excludes_second_worker_and_close_stops_claims(audio):
    fixture, _, provider = audio
    sid, _, _ = seed(fixture, 32)
    clock = SimpleNamespace(value=1000)
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    worker = AnnotationSampleWorker(fixture.core.people, lease_renew_interval=0.01, maximum_run_seconds=5)
    original_embed = provider.embed
    original_renew = AnnotationSampleRepositoryMixin.renew_sample_job

    def embed(tracks):
        entered.set()
        assert release.wait(5)
        return original_embed(tracks)

    def renew(repository, session, generation, token, now, **kwargs):
        result = original_renew(repository, session, generation, token, now, **kwargs)
        renewed.set()
        return result

    with patch('allday_asr.v3.application.annotation_samples.time.time', side_effect=lambda: clock.value), patch.object(provider, 'embed', side_effect=embed), patch.object(AnnotationSampleRepositoryMixin, 'renew_sample_job', renew), ThreadPoolExecutor(1) as pool:
        pending = pool.submit(worker.run_pending, 1)
        try:
            assert entered.wait(3)
            for _ in range(4):
                renewed.clear()
                clock.value += 50
                assert renewed.wait(1)
                with SqliteUnitOfWork(fixture.core.database).reading() as uow:
                    assert uow.people.sample_job(sid)['lease_until'] >= clock.value + 120
            # 200 seconds have elapsed, beyond the original 120-second lease.
            assert not AnnotationSampleWorker(fixture.core.people).run_pending(1)
        finally:
            release.set()
        assert pending.result(timeout=3)[0]['status'] == 'processed'
        with SqliteUnitOfWork(fixture.core.database) as uow:
            uow.people.enqueue_samples(sid)
        worker.close()
        assert not worker.run_pending(1)
        with SqliteUnitOfWork(fixture.core.database).reading() as uow:
            assert uow.people.sample_job(sid)['status'] == 'queued'
            assert worker.health()['queued'] == 1
