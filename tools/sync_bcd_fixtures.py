"""Synthetic fixtures only, using production annotation/sample/correction services."""
import io
import math
import struct
import wave

from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork
from allday_asr.v3.application.durable_processing import CorrectUtteranceCommand
from tests.test_v34_open_speaker_identity import FakeEmbeddingProvider, _track_id, _utterance_id, _session_id, NOW_TEXT


class SyntheticProvider(FakeEmbeddingProvider):
    def embed(self, tracks):
        for track in tracks:
            self.vectors[track.speaker_track_id] = (1., 0., 0.)
        return super().embed(tracks)


def add_review(fixture, number):
    if not isinstance(fixture.provider, SyntheticProvider):
        fixture.provider = SyntheticProvider({})
        fixture.core.people._provider = fixture.provider
    fixture._seed_track(number)
    fixture.provider.vectors[_track_id(number)] = (1., 0., 0.)
    output = io.BytesIO()
    with wave.open(output, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b''.join(struct.pack('<h', int(4000 * math.sin(2 * math.pi * (220 + number * 110) * i / 16000))) for i in range(160000)))
    stored = fixture.core.audio_store.put_bytes(output.getvalue())
    with SqliteUnitOfWork(fixture.core.database) as u:
        u.evidence.connection.execute('UPDATE audio_replicas SET storage_key=? WHERE asset_id=?', (stored.storage_key, f'asset-{number}'))
        u.changes.append('recording_session', _session_id(number), 1, 'upsert',
                         {'session_id': _session_id(number), 'state': 'ready_for_processing', 'captured_start': NOW_TEXT})
    fixture.core.corrections.classify_segments([{'utterance_id': _utterance_id(number), 'revision': 1}], 'live_speech')
    fixture.core.people.assign_utterances([{'utterance_id': _utterance_id(number), 'revision': 2}],
        person_id=None, display_name=f'Synthetic reviewer {number}', actor='isolated-fixture')
    jobs = fixture.core.people.sample_worker.run_pending()
    if not jobs or jobs[-1]['status'] != 'processed':
        raise RuntimeError('synthetic sample did not process')


def change_evidence(fixture, number=2):
    with SqliteUnitOfWork(fixture.core.database) as u:
        row = u.evidence.get_utterance(_utterance_id(number))
    fixture.core.corrections.correct_utterance(CorrectUtteranceCommand(
        row.utterance_id, row.revision, 'Synthetic corrected evidence', 'isolated-fixture'))
    fixture.core.people.sample_worker.run_pending()
