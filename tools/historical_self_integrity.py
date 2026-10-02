"""Private read-only fingerprints; truth is hashed, never used for inference."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from short_self_dataset import assets

PROTECTED = {'persons', 'speaker_tracks', 'processing_runs', 'stage_runs', 'stage_attempts',
    'voice_prototypes', 'voice_prototype_reviews', 'person_profile_revisions',
    'person_cluster_links', 'speaker_clusters', 'speaker_cluster_memberships',
    'speaker_cluster_runs', 'speaker_match_decisions', 'person_identity_policy_revisions',
    'session_dataset_roles', 'session_role_audit', 'session_learning_exposure', 'holdout_openings',
    'dataset_reservation_settings', 'dataset_setting_audit', 'annotation_input_revision'}


def snapshot(state):
    state = Path(state).resolve()
    c = sqlite3.connect((state/'core.sqlite3').as_uri()+'?mode=ro', uri=True)
    try:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        tables = {}
        for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            rows = sorted([tuple(r) for r in c.execute('SELECT * FROM "'+name+'"')], key=str)
            tables[name] = {'rows': len(rows), 'sha256': hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()}
        protected = {k:v for k,v in tables.items() if k in PROTECTED or k.startswith(
            ('blind_', 'purity_', 'speaker_purity_', 'speaker_source_purity_', 'speaker_profile_purity_',
             'speaker_research_', 'session_speaker_', 'speaker_enrollment_', 'annotation_'))}
        return {'all_tables': tables, 'protected_tables': protected, 'assets': assets(state)}
    finally:
        c.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--state-dir', type=Path, default=Path('state/v3'))
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(snapshot(args.state_dir), indent=2), encoding='utf-8')
