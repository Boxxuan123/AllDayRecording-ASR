from copy import deepcopy

import pytest

from tests.test_review_audio_boundaries import actual_audio as actual_audio, people as people, pending_sample
from tests.test_phase2_samples import short_rows
from allday_asr.v3.interfaces.device_reviews import DeviceReviewService

from allday_asr.v3.interfaces.review_evidence import candidate_evidence
from allday_asr.v3.interfaces.transfer.devices import DeviceConflictError
from tests.test_v3_device_reviews import _service


def test_media_windows_map_only_real_overlaps_not_81_track_sentences():
    candidate = {'session_id': 's', 'representative_clips': [
        {'media_id': 'm', 'start_ms': i * 1000, 'end_ms': i * 1000 + 500} for i in range(5)]}
    detail = {'segments': [{'media_id': 'm', 'source_start_ms': 0, 'source_end_ms': 81000,
        'session_start_ms': 10000, 'session_end_ms': 91000}], 'utterances': [
        {'utterance_id': f'u{i}', 'session_id': 's', 'revision': 7, 'status': 'active',
         'start_ms': 10000 + i * 1000, 'end_ms': 10900 + i * 1000} for i in range(81)]}
    evidence = candidate_evidence(candidate, detail)
    assert [e['utterance_id'] for e in evidence] == [f'u{i}' for i in range(5)]
    assert all(e['revision'] == 7 for e in evidence)
    assert evidence[0]['session_start_ms'] == 10000
    assert evidence[0]['utterance_end_ms'] == 10900  # complete sentence != 500 ms crop
    second = deepcopy(candidate)
    second['representative_clips'] = [{'media_id': 'm', 'start_ms': 10000, 'end_ms': 10500}]
    assert [e['utterance_id'] for e in candidate_evidence(second, detail)] == ['u10']
    detail['segments'].append(deepcopy(detail['segments'][0]))
    assert candidate_evidence(candidate, detail) == []  # ambiguous reuse fails closed


def test_missing_mapping_does_not_guess_or_disable_history():
    service, _ = _service()
    candidates = service.snapshot()['items'][0]['context']['voice_candidates']
    assert candidates[0]['evidence_utterances'] == []
    assert candidates[0]['review_key']


def test_changed_candidate_guard_prevents_stale_or_repeated_decision():
    service, people = _service()
    item = service.snapshot()['items'][0]
    candidate = item['context']['voice_candidates'][0]
    people.candidates[1]['review_status'] = 'uncertain'
    with pytest.raises(DeviceConflictError):
        service.resolve('test', {'review_id': item['review_id'], 'prototype_id': candidate['prototype_id'],
            'action': 'confirm', 'expected_review_key': candidate['review_key']})
    assert not people.reviews


def test_two_candidates_decide_one_never_the_group():
    service, people = _service()
    item = service.snapshot()['items'][0]
    candidate = item['context']['voice_candidates'][0]
    response = service.resolve('test', {'review_id': item['review_id'], 'prototype_id': candidate['prototype_id'],
        'action': 'confirm', 'expected_review_key': candidate['review_key']})
    assert len(people.reviews) == 1
    assert len(response['reviews']['items'][0]['context']['voice_candidates']) == 1

# Real SQLite/capture/desktop-query integration, in addition to the pure mapper.


def test_real_snapshot_maps_only_candidate_sentences(actual_audio):
    fixture, _, _ = actual_audio
    ids = short_rows(fixture)
    _, sample = pending_sample(fixture, ids[:2])
    service = DeviceReviewService(fixture.core)
    candidates = [c for item in service.snapshot()['items']
                  for c in item['context'].get('voice_candidates', [])]
    candidate = next(c for c in candidates if c['prototype_id'] == sample['prototype_id'])
    assert {row['utterance_id'] for row in candidate['evidence_utterances']} == set(ids[:2])
    assert ids[2] not in {row['utterance_id'] for row in candidate['evidence_utterances']}
