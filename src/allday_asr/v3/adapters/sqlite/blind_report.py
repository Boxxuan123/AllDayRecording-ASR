"""Private event-level evaluation; revisions append evaluations and replace exports."""

import json
from collections import Counter

from allday_asr.v3.adapters.sqlite.blind_reviews import latest_truth
from allday_asr.v3.adapters.sqlite.dataset_reservations import now, settings
from allday_asr.v3.application.blind_scoring import digest, encoded, metrics


def build_report(connection, experiment_id):
    experiment = connection.execute('SELECT * FROM blind_experiments WHERE experiment_id=?', (experiment_id,)).fetchone()
    if experiment is None:
        return {'status': 'EXPERIMENT_NOT_INITIALIZED', 'settings': settings(connection)}
    frozen = json.loads(experiment['snapshot_json'])
    known = set(frozen['matcher']['known_ids'])
    self_id = frozen.get('self_person_id')
    queries = {r['query_id']: json.loads(r['query_json']) for r in connection.execute('SELECT * FROM blind_query_views WHERE experiment_id=?', (experiment_id,))}
    predictions = {r['query_id']: json.loads(r['prediction_json']) | {'prediction_created_at': r['prediction_created_at'],
        'prediction_snapshot_id': r['prediction_snapshot_id']} for r in connection.execute('SELECT * FROM blind_prediction_snapshots WHERE experiment_id=?', (experiment_id,))}
    truth = latest_truth(connection)
    tasks = {r['query_id']: dict(r) for r in connection.execute('SELECT t.* FROM blind_review_tasks t JOIN blind_events e USING(event_id) WHERE e.experiment_id=?', (experiment_id,))}
    events = [dict(r) for r in connection.execute('SELECT * FROM blind_events WHERE experiment_id=? AND superseded_by IS NULL ORDER BY event_id', (experiment_id,))]
    rows, diagnostics = [], []
    completed, unresolved = 0, 0
    for event in events:
        query_id = event['canonical_query_id']
        task = tasks.get(query_id)
        review = truth.get(task['task_id']) if task else None
        for view_id in json.loads(event['query_ids_json']):
            diagnostics.append({'event_id': event['event_id'], 'query': queries[view_id],
                                'prediction': predictions[view_id],
                                'truth': review if view_id == query_id and review and review['action'] == 'submit' else None,
                                'label': 'diagnostic; correlated view; unreviewed views unscored'})
        if review is None or review['action'] != 'submit':
            continue
        completed += 1
        if review['unknown_kind'] in {'dont_know', 'inaudible'}:
            unresolved += 1
            continue
        query = queries[query_id]
        result = {'event_id': event['event_id'], 'query_id': query_id, 'session_id': query['session_id'],
                  'date': query['date'], 'truth': review, 'prediction': predictions[query_id],
                  'query_view_count': len(json.loads(event['query_ids_json']))}
        revision_hash = digest([review, event['query_ids_json'], predictions[query_id]])
        connection.execute('INSERT OR IGNORE INTO blind_evaluations VALUES(?,?,?,?,?)',
            (digest([event['event_id'], revision_hash]), event['event_id'], revision_hash, encoded(result), now()))
        rows.append(result)
        # Only the heard representative has ground truth. Other views are retained
        # with predictions, never silently assigned this primary person's identity.
    all_metrics = metrics(rows, known, self_id)
    strata = {p: metrics([r for r in rows if r['truth']['purity'] == p], known, self_id)
              for p in ('clean_single', 'mixed_overlap', 'boundary_cross', 'uncertain')}
    composition_strata = {p: metrics([r for r in rows if r['truth'].get('speaker_composition') == p], known, self_id)
        for p in ('clean_single', 'simultaneous_overlap', 'sequential_multi_speaker', 'backchannel', 'uncertain')}
    boundary_strata = {p: metrics([r for r in rows if r['truth'].get('boundary_quality') == p], known, self_id)
        for p in ('clean', 'cut', 'uncertain')}
    people = Counter(r['truth']['primary_person_id'] or 'stranger' for r in rows)
    person_sessions = {p: len({r['session_id'] for r in rows if (r['truth']['primary_person_id'] or 'stranger') == p}) for p in people}
    coverage = {'independent_events': len(rows), 'generated_events': len(events), 'query_views': len(queries),
                'reviewed_events': completed, 'pending_review': len(events) - completed,
                'unresolved_truth_events': unresolved, 'sessions': len({r['session_id'] for r in rows}),
                'dates': len({r['date'] for r in rows}), 'known_persons': len(set(people) & known),
                'unknown_library_persons': len(set(people) - known - {'stranger'}),
                'unknown_kind_counts': dict(Counter(r['truth']['unknown_kind'] for r in rows)),
                'person_event_counts': dict(people), 'person_session_counts': person_sessions,
                'self_hard_negatives': sum(r['truth']['primary_person_id'] == self_id for r in rows) if self_id else 0}
    gates = frozen['evidence_requirements']
    sufficient = (len(rows) >= gates['events'] and coverage['sessions'] >= gates['sessions']
                  and coverage['dates'] >= gates['dates'] and all(people[p] >= gates['events_per_known']
                    and person_sessions.get(p, 0) >= gates['sessions_per_known'] for p in known)
                  and all_metrics['legacy_G']['unknown'] >= gates['unknown_events']
                  and coverage['self_hard_negatives'] >= gates['self_events'])
    comparison = {}
    for rule in ('G', 'P'):
        a, b = all_metrics['legacy_' + rule], all_metrics['clean_' + rule]
        comparison[rule] = {k: {'legacy': a[k], 'clean': b[k],
            'clean_minus_legacy': b[k]['rate'] - a[k]['rate'] if a[k]['rate'] is not None else None}
            for k in ('known_recall', 'wrong_known_rate', 'unknown_FA', 'self_to_other_rate')}
    roles = [dict(r) for r in connection.execute('SELECT * FROM session_dataset_roles')]
    jobs = [dict(r) for r in connection.execute('SELECT session_id,experiment_id,status,attempts,error,updated_at FROM blind_shadow_jobs WHERE experiment_id=?', (experiment_id,))]
    progress = {'reserved_sessions': sum(r['dataset_role'] == 'blind' for r in roles),
                'processed_sessions': sum(r['status'] == 'completed' for r in jobs),
                'job_status_counts': dict(Counter(r['status'] for r in jobs)), 'jobs': jobs, **coverage,
                'known': all_metrics['legacy_G']['known'], 'unknown': all_metrics['legacy_G']['unknown'],
                'legacy_errors': {rule: all_metrics['legacy_'+rule]['wrong_known'] + all_metrics['legacy_'+rule]['unknown_false_accept'] for rule in ('G','P')},
                'clean_errors': {rule: all_metrics['clean_'+rule]['wrong_known'] + all_metrics['clean_'+rule]['unknown_false_accept'] for rule in ('G','P')}}
    report = {'status': 'BLIND_EVIDENCE_AVAILABLE' if sufficient else 'INSUFFICIENT BLIND EVIDENCE',
              'experiment_id': experiment_id, 'experiment_hash': experiment['snapshot_hash'],
              'updated_at': now(), 'settings': settings(connection), 'progress': progress,
              'primary_unit': 'independent event; fixed representative query',
              'event_metrics': all_metrics, 'purity_strata': strata, 'comparison': comparison,
              'speaker_composition_strata': composition_strata, 'boundary_quality_strata': boundary_strata,
              'review_schema_version': frozen.get('review_schema_version', 1),
              'projection_version': frozen.get('projection_version', 'native-token-grouping-v1'),
              'query_builder_version': frozen.get('query_builder_version', 'longest5-head8s-v1'),
              'denominator_policy': 'this experiment only; V1 and V2 never pooled',
              'evidence_requirements': gates,
              'query_view_metrics': {'label': 'diagnostic only; only individually heard canonical views scored',
                                     'metrics': metrics(rows, known, self_id)},
              'query_purity_gate': 'NOT ENABLED', 'production_mode': 'legacy',
              'warning': 'No winner or deployment recommendation. Intervals are event-level Wilson summaries; session concentration remains visible.'}
    return report, {'experiment.json': dict(experiment) | {'snapshot': frozen}, 'session-roles.json': roles,
                    'blind-events.json': events, 'prediction-snapshots.json': predictions,
                    'review-progress.json': progress, 'event-level-results.json': rows,
                    'query-view-diagnostics.json': diagnostics, 'current-report.json': report,
                    'verification.json': {'experiment_hash': experiment['snapshot_hash'],
                        'prediction_count': len(predictions), 'all_predictions_precede_truth': all(
                            r['prediction']['prediction_created_at'] < r['truth']['reviewed_at'] for r in rows),
                        'probe_filter_count': 0, 'reserved_accepted_prototypes': connection.execute("""SELECT COUNT(*) FROM voice_prototypes p
                            JOIN speaker_tracks t USING(speaker_track_id) JOIN session_dataset_roles r USING(session_id)
                            WHERE p.status='accepted' AND r.dataset_role!='learning'""").fetchone()[0]}}


def markdown(report):
    lines = ['# Blind Shadow Validation — Private', '', report['status'], '',
             'Primary unit: independent event; correlated views are diagnostic. No deployment recommendation.', '']
    if 'event_metrics' not in report:
        return '\n'.join(lines)
    lines += [f"Experiment: {report['experiment_id']}; hash: {report['experiment_hash']}", '',
              f"Coverage: {encoded(report['progress'])}", '',
              '| Arm/rule | Known correct | Known reject | Wrong known | Unknown reject | Unknown FA | Self→other |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for arm, m in report['event_metrics'].items():
        lines.append(f"| {arm} | {m['known_correct']}/{m['known']} | {m['known_reject']} | {m['wrong_known']} | "
                     f"{m['unknown_correct_reject']} | {m['unknown_false_accept']}/{m['unknown']} | {m['self_to_other']}/{m['self']} |")
    lines += ['', 'Rates, denominators and 95% Wilson intervals:', '', '```json',
              json.dumps(report['comparison'], ensure_ascii=False, indent=2), '```', '',
              'Purity strata and view diagnostics are in current-report.json. Purity never filters the primary metric.',
              '“不知道/听不清” completes review but leaves identity truth unresolved; those events are counted in unresolved coverage.',
              'Production matcher/profile, CAM++, G/P and margin remain frozen. Query purity gate is NOT ENABLED.', '']
    return '\n'.join(lines)
