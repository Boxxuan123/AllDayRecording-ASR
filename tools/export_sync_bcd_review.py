"""Export selected committed text only. Run after both repositories are committed.

python tools/export_sync_bcd_review.py --phone <repo> --output outputs/sync-bcd-final-review.zip
The archive is local evidence, never a publishable repository artifact.
"""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def check_text(name, data):
    text = data.decode('utf-8')
    # Permit parser source mentioning a PEM delimiter, never a PEM body.
    if '\x00' in text or re.search(r'-----BEGIN (?:[A-Z ]*PRIVATE KEY|CERTIFICATE)-----\s+[A-Za-z0-9+/=]{32,}', text):
        raise ValueError(f'Binary or certificate material: {name}')
    if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16})\b', text):
        raise ValueError(f'Possible credential: {name}')


def selected(label, path):
    if label == 'phone':
        return (path.endswith('.ets') and path.startswith((
            'phone/src/main/ets/', 'phone/src/test/', 'common/src/main/ets/'))
            or path.startswith('tools/quality/') and path.endswith(('.cjs', '.mjs', '.ps1', '.ets', '.md', '.py'))
            or path in ('tests/ui/run_bcd.py', 'tests/ui/testcases/PhoneSyncBCD.py', 'tests/ui/testcases/PhoneSyncBCD.json', 'doc/SYNC_OPTIMIZATION_FINAL_ACCEPTANCE.md', 'contracts/v3/source.json', 'tests/ui/main.py', 'tests/ui/run_phase2a.py', 'tests/ui/run_phase1.py',
                        'tests/ui/testcases/PhonePhase2Production.py', 'tests/ui/testcases/PhoneOfflineAnnotation.py',
                        'tests/ui/testcases/PhoneSyncInteraction.py', 'doc/SYNC_PHASE2A_ACCEPTANCE.md',
                        'doc/SYNC_PHASE2A_CLOSEOUT.md', 'doc/CURRENT_ARCHITECTURE.md',
                        'phone/src/main/module.json5', 'code-linter.json5'))
    return (path.startswith('src/allday_asr/v3/') and path.endswith('.py')
            or path.startswith('tests/') and path.endswith('.py') and any(key in path for key in (
                'transfer', 'device', 'review_audio', 'phone_voice', 'sync_phase2a', 'v34_open_speaker', 'v33_intelligent', 'phase1_human_facts', 'phase2_samples', 'v3_contracts', 'v3_bootstrap', '__init__'))
            or path.startswith('contracts/v3/') and path.endswith('.json')
            or path in ('tools/sync_bcd_fixtures.py', 'docs/sync-optimization-final-acceptance.md', 'tools/sync_phase1_test_receiver.py', 'tools/sync_phase2a_device_receiver.py',
                        'tools/export_sync_bcd_review.py', 'docs/sync-phase2a-acceptance.md',
                        'docs/sync-phase2a-closeout.md'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phone', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    desktop = Path(__file__).resolve().parents[1]
    output = args.output.resolve()
    if not output.is_relative_to(desktop / 'outputs'):
        parser.error('output must stay in the ignored desktop outputs directory')
    if subprocess.run(['git', '-C', str(desktop), 'check-ignore', '--quiet', str(output)]).returncode:
        parser.error('output is not ignored')
    versions = {}
    entries = {}
    for label, root, baseline in (
        ('phone', args.phone, '283f7696a9883a504159275608eceea882fcd7c0'),
        ('desktop', desktop, '9516a432d0a8f65d748013b29c7de5154efea1c3'),
    ):
        head = git(root, 'rev-parse', 'HEAD').decode().strip()
        git(root, 'merge-base', '--is-ancestor', baseline, head)
        tracked = git(root, 'ls-tree', '-r', '--name-only', head).decode().splitlines()
        files = [p for p in tracked if selected(label, p)]
        # Baseline selections also retain deleted files in the patch.
        old = git(root, 'ls-tree', '-r', '--name-only', baseline).decode().splitlines()
        paths = sorted(set(files + [p for p in old if selected(label, p)]))
        for name in files:
            data = git(root, 'show', f'{head}:{name}')
            check_text(name, data)
            entries[f'{label}/{name}'] = data
        patch = git(root, 'diff', '--no-ext-diff', '--no-renames', baseline, head, '--', *paths)
        check_text(label + '.diff', patch)  # Includes removed lines, not just additions.
        entries[f'diffs/{label}.diff'] = patch
        versions[label] = {'repository': git(root, 'remote', 'get-url', 'origin').decode().strip(),
                           'head': head, 'baseline': baseline, 'baseline_is_ancestor': True,
                           'files': len(files)}
    manifest = {'repositories': versions, 'files': {name: digest(data) for name, data in sorted(entries.items())},
                'scope': 'Committed selected source/tests/docs and round B/C/D text diffs. No runtime data or HAP.',
                'reproduce': 'python tools/export_sync_bcd_review.py --phone <phone-repo> --output outputs/sync-bcd-final-review.zip'}
    entries['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name, expected in manifest['files'].items():
            assert digest(archive.read(name)) == expected
    print(json.dumps({'path': str(output), 'sha256': digest(output.read_bytes()),
                      'entries': len(entries), 'repositories': versions}, indent=2))


if __name__ == '__main__':
    main()
