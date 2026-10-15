"""Verify the eight audit regressions against an isolated Git baseline."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

SELECTION = ' or '.join((
    'test_r1_model_output_cannot_publish_after_real_correction',
    'test_r1_pending_proposal_cannot_rebind_new_revision',
    'test_r2_same_session_new_revision_after_batch_deadline',
    'test_r4_unacknowledged_request_survives_consume_and_restart',
    'test_r3_real_composition_sync_never_refreshes_all_history',
))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', default='87532be')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (args.output or root / 'outputs' / ('engineering-baseline-' + datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    subprocess.run(['git', 'archive', '--format=zip', '--output=' + str(output / 'baseline.zip'),
                    args.baseline, '--', 'src', 'tests', 'pyproject.toml'], cwd=root, check=True)
    checkout = output / 'checkout'
    with zipfile.ZipFile(output / 'baseline.zip') as archive:
        archive.extractall(checkout)
    shutil.copyfile(root / 'tests' / 'test_engineering_refactor.py', checkout / 'tests' / 'test_engineering_refactor.py')
    # Apply only test-lifetime support; preserve baseline production behavior.
    for support in ('conftest.py', 'temp_workspace.py'):
        shutil.copyfile(root / 'tests' / support, checkout / 'tests' / support)
    command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q', 'tests/test_engineering_refactor.py',
               '-k', SELECTION, '--basetemp', str(output / 'pytest-temp'), '--junitxml', str(output / 'pytest.xml')]
    with (output / 'pytest.stdout.log').open('wb') as stdout, (output / 'pytest.stderr.log').open('wb') as stderr:
        process = subprocess.run(command, cwd=checkout, stdout=stdout, stderr=stderr, check=False,
            env={**os.environ, 'PYTHONPATH': str(checkout / 'src'), 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    suites = ElementTree.parse(output / 'pytest.xml').getroot()
    cases = list(suites.iter('testcase'))
    failures = sum(case.find('failure') is not None for case in cases)
    errors = sum(case.find('error') is not None for case in cases)
    verified = process.returncode == 1 and len(cases) == failures == 8 and errors == 0
    result = {'baseline': args.baseline, 'command': command, 'exit_code': process.returncode,
              'case_count': len(cases), 'failures': failures, 'errors': errors, 'old_defects_reproduced': verified}
    (output / 'results.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return int(not verified)


if __name__ == '__main__':
    raise SystemExit(main())
