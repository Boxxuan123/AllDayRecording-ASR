"""Eligibility uses first reserved admission, independent of capture time/truth."""

from datetime import datetime, timezone

ADMISSION_POLICY = 'first-admission-after-freeze-v2'
CAPTURE_TIME_BLOCK = 'historical audio predates experiment'


def instant(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def admission(connection, session_id, role, frozen, queries, previous):
    exposures = connection.execute(
        'SELECT COUNT(*) FROM session_learning_exposure WHERE session_id=?', (session_id,)
    ).fetchone()[0]
    seen = set(frozen['seen_audio_sha256'])
    reused = sorted({w['sha256'] for q in queries for w in q['windows'] if w['sha256'] in seen})
    facts = connection.execute("""SELECT COUNT(*) FROM annotation_facts f
        JOIN utterances u ON u.utterance_id=f.source_utterance_id
        WHERE u.session_id=? AND f.dimension='person'""", (session_id,)).fetchone()[0]
    evidence = {'policy_version': ADMISSION_POLICY, 'role_assigned_at': role['role_assigned_at'],
                'experiment_cutoff': frozen['data_cutoff'], 'learning_exposure_count': exposures,
                'previously_seen_audio_sha256': reused, 'prior_person_fact_count': facts}
    if previous and previous['status'] == 'blocked' and previous['error'] == CAPTURE_TIME_BLOCK:
        evidence['previous_decision'] = {'policy_version': 'capture-time-cutoff-v1',
            'status': previous['status'], 'reason': previous['error'], 'updated_at': previous['updated_at']}
    error = ('human person facts preceded new prediction' if facts and queries else
             'session learning exposure preceded blind prediction' if exposures else
             'session admission predates experiment' if instant(role['role_assigned_at']) < instant(frozen['data_cutoff']) else
             'original media already used before experiment' if reused else
             'no usable automatic speaker queries' if not queries else None)
    # Store the decision with each frozen query; never edit the experiment/model.
    evidence['decision'] = 'blocked' if error else 'eligible'
    evidence['reason'] = error
    return evidence, error
