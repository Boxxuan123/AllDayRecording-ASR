"""Build revisioned evidence snapshots from existing timeline identity references."""
from .evidence_identity import project_evidence_identity as _utterance


def daily_inputs(uow, start: str, end: str) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    sources = uow.insights.daily_utterances(start, end)
    tasks = uow.insights.daily_tasks(tuple(row['utterance_id'] for row in sources))
    references = uow.insights.daily_identity_references(tuple(dict.fromkeys(row['speaker_track_id'] for row in sources)))
    resolved_rows = [(row, _utterance(row, references.get(row['speaker_track_id'], {}))) for row in sources]
    names = uow.insights.daily_person_names(tuple(dict.fromkeys(value['speaker_person_id'] for _, value in resolved_rows if value['speaker_person_id'])))
    task_ids = {}
    for task in tasks:
        for identifier in task['evidence_ids']:
            task_ids.setdefault(identifier, []).append(task['event_id'])
    rows = []
    for row, resolved in resolved_rows:
        ref = references.get(row['speaker_track_id'], {})
        if resolved['identity'] == 'self':
            participant = {'key': 'self', 'kind': 'self', 'label': '本人',
                           'status': 'source_confirmed', 'confidence': None}
        elif resolved['speaker_person_id']:
            pid = resolved['speaker_person_id']
            participant = {'key': pid, 'person_id': pid, 'kind': 'known',
                           'label': names[pid], 'status': 'source_confirmed',
                           'confidence': None}
        else:
            track = row['speaker_track_id']
            cluster = ref.get('speaker_cluster_id')
            participant = {'key': cluster or track or 'unresolved', 'kind': 'unknown',
                           'label': '未知说话人', 'status': 'unresolved', 'confidence': None,
                           'speaker_track_id': track, 'cluster_id': cluster}
        rows.append({k: row[k] for k in ('utterance_id', 'revision', 'session_id',
                    'start_ms', 'end_ms', 'start_at', 'end_at', 'text', 'audio_ranges')} |
                    {'participant': participant, 'identity_at_generation': resolved['identity'],
                     'task_ids': sorted(task_ids.get(row['utterance_id'], []))})
    return tuple(rows), tasks
