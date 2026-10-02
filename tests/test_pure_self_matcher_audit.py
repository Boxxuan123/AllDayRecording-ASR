"""Audit provenance, event independence and read-only pure matcher contracts."""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
from audit_pure_self_matcher import crop_ranges, pure_infer, invariance, run
from build_pure_self_eval_manifest import cohort_for, independence, leak_flags, strict_review
from allday_asr.v3.adapters.self_identity import CalibratedSelfIdentityMatcher


def flags(**changes):
    return dict.fromkeys(['enrollment', 'calibration_positive', 'calibration_negative',
                         'profile_learning', 'prior_diagnostic_fitting'], False) | changes


@pytest.mark.parametrize('source', ['enrollment', 'calibration_positive', 'calibration_negative'])
def test_enrollment_and_calibration_never_enter_eval(source):
    assert cohort_for(flags(**{source: True})) is None


def test_profile_and_previous_fitting_are_separate_diagnostics():
    assert cohort_for(flags()) == 'primary'
    assert cohort_for(flags(profile_learning=True)) == 'secondary_exposed'
    assert cohort_for(flags(prior_diagnostic_fitting=True)) == 'secondary_exposed'


def test_leakage_by_audio_content_and_intersecting_boundary():
    event = {'sha256': 'same', 'start_ms': 2000, 'end_ms': 4000}
    calibration = [{'sha256': 'same', 'start_ms': 3000, 'end_ms': 5000, 'identity': 'self'}]
    assert leak_flags(event, set(), calibration, [], [])['calibration_positive']
    event['start_ms'], event['end_ms'] = 5000, 7000
    assert not leak_flags(event, set(), calibration, [], [])['calibration_positive']
    assert leak_flags(event, set(), calibration, [], [])['calibration_source_file_exposure']
    assert cohort_for(leak_flags(event, set(), calibration, [], [])) == 'secondary_exposed'
    event['sha256'] = 'different'
    assert not leak_flags(event, set(), calibration, [], [])['calibration_positive']


def review(person='self', purity='clean_single', conflicting=0):
    return {'review_primary_person_id': person, 'purity': purity, 'conflicting': conflicting,
            'review_references_json': json.dumps([{'action': 'submit', 'purity': purity,
                                                  'primary_person_id': person}])}


def test_individual_clean_review_overrides_old_cluster_identity():
    row = review()
    row['old_cluster_person'] = 'other'
    assert strict_review(row, 'self') == 'self'
    assert strict_review(review('other'), 'self') == 'non-self'


@pytest.mark.parametrize('mode', ['automatic', 'batch_human_only', 'conflict', 'mixed'])
def test_non_individual_or_non_clean_truth_is_not_promoted(mode):
    row = review()
    if mode in {'automatic', 'batch_human_only'}:
        row['review_references_json'] = '[]'
    elif mode == 'conflict':
        row['conflicting'] = 1
    else:
        row['purity'] = 'mixed_overlap'
    assert strict_review(row, 'self') is None


def test_crops_respect_manual_boundaries_and_do_not_create_independent_events():
    crops = crop_ranges(5000, 13000)
    assert [hi-lo for _,lo,hi in crops] == [8000, 1500, 2000, 3000, 4000, 6000]
    assert all(5000 <= lo < hi <= 13000 for _,lo,hi in crops)
    events = [{'event_id': str(i), 'session_id': 's', 'session_start_ms': lo,
               'session_end_ms': hi, 'utterance_ids': ['u'], 'sha256': 'sha',
               'start_ms': lo, 'end_ms': hi} for i,(_,lo,hi) in enumerate(crops)]
    assert len({e['independence_group'] for e in independence(events)}) == 1


@pytest.mark.parametrize('bounds', [(-1, 8000), (0, 999), (5000, 5000)])
def test_invalid_manual_crop_rejected(bounds):
    with pytest.raises(ValueError):
        crop_ranges(*bounds)


@pytest.fixture
def matcher(tmp_path):
    state = tmp_path/'state'
    identity = state/'identity'
    identity.mkdir(parents=True)
    voiceprint = identity/'self.npz'
    np.savez(voiceprint, embeddings=np.array([[1., 0.], [1., 0.]]), centroid=np.array([1., 0.]))
    policy = {'accepted': True, 'blockers': [], 'policy_version': 'synthetic', 'self_threshold': .5,
              'not_self_threshold': -.5, 'false_accept_rate': 0, 'false_reject_rate': 0,
              'voiceprint': str(voiceprint), 'voiceprint_sha256': hashlib.sha256(voiceprint.read_bytes()).hexdigest()}
    (identity/'active-self-identity-policy.json').write_text(json.dumps(policy), encoding='utf-8')
    return CalibratedSelfIdentityMatcher(state), state


class Backend:
    def extract_speaker_embeddings(self, waves, *, batch_size=8):
        return np.array([[1., 0.] if w.mean() > 0 else [-1., 0.] for w in waves], dtype=np.float32)


def test_pure_path_without_ownership_named_matcher_or_production_writes(matcher, monkeypatch):
    import allday_asr.v3.adapters.sqlite.product_self_queries as queries
    import allday_asr.v3.domain.speaker_turns as turns
    import allday_asr.v3.adapters.sqlite.people_repository as people

    def forbidden(*args, **kwargs):
        raise AssertionError('pure path crossed into production/profile')

    monkeypatch.setattr(queries, 'product_self_queries', forbidden)
    monkeypatch.setattr(turns, 'foreign_turns', forbidden)
    monkeypatch.setattr(people.SqlitePeopleRepository, 'person_vectors', forbidden)
    actual, state = matcher
    before = {str(p): p.read_bytes() for p in state.rglob('*') if p.is_file()}
    results = pure_infer([np.ones(32000), -np.ones(32000), np.ones(16000)], [2000, 2000, 1000], Backend(), actual)
    assert [r['decision'] for r in results] == ['self', 'not_self', 'unknown']
    assert results[2]['score'] == 1.0 and results[2]['reason'] == 'insufficient_track_quality'
    assert before == {str(p): p.read_bytes() for p in state.rglob('*') if p.is_file()}


def test_companion_order_and_repeat_invariance_contract(matcher):
    actual, _ = matcher
    waves = [np.ones(32000), -np.ones(96000)]
    result = invariance(waves, [2000, 6000], Backend(), actual, [0, 1])
    assert result['max_embedding_drift'] == result['max_score_drift'] == result['decision_flips'] == 0


def test_changed_frozen_manifest_rejected_before_model_load(tmp_path):
    (tmp_path/'manifest.json').write_text('{"format":"pure-self-human-audit-v1"}', encoding='utf-8')
    (tmp_path/'manifest.sha256').write_text('bad', encoding='ascii')
    with pytest.raises(ValueError, match='changed'):
        run(tmp_path/'state', tmp_path)


def test_output_inside_state_rejected_without_write(tmp_path):
    with pytest.raises(ValueError, match='outside'):
        run(tmp_path, tmp_path/'identity')
