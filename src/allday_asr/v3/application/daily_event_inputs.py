"""Build revisioned evidence snapshots from existing timeline identity references."""
from .event_extraction import _utterance


def daily_inputs(uow, start: str, end: str) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    sources = uow.insights.daily_utterances(start, end)
    tasks = uow.insights.daily_tasks(tuple(row['utterance_id'] for row in sources))
    references = {}
    for sid in dict.fromkeys(row['session_id'] for row in sources):
        detail = uow.desktop.session_detail(sid)
        references.update({r['speaker_track_id']: r for r in detail['speaker_tracks']})
    rows = []
    for row in sources:
        ref = references.get(row['speaker_track_id'], {})
        resolved = _utterance(row, ref)
        if resolved['identity'] == 'self':
            participant = {'key': 'self', 'kind': 'self', 'label': '本人',
                           'status': 'source_confirmed', 'confidence': None}
        elif resolved['speaker_person_id']:
            pid = resolved['speaker_person_id']
            profile = uow.insights.person_profile(pid)
            participant = {'key': pid, 'person_id': pid, 'kind': 'known',
                           'label': profile['display_name'], 'status': 'source_confirmed',
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
                     'task_ids': sorted(t['event_id'] for t in tasks
                                        if row['utterance_id'] in t['evidence_ids'])})
    return tuple(rows), tasks
