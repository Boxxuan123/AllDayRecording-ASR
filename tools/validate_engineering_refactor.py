"""Run the complete local engineering gate with fresh, per-command evidence."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phone', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = (args.output or root / 'outputs' / ('engineering-refactor-' + datetime.now().strftime('%Y%m%d-%H%M%S'))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    front = root / 'all_day_recording_front'
    npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
    node = shutil.which('node')
    if not npm or not node:
        parser.error('Node and npm are required; no checks were skipped')
    checks = [
        ('pytest', [sys.executable, '-X', 'utf8', '-m', 'pytest', '-q', '--basetemp', str(output / 'pytest-temp'), '--junitxml', str(output / 'pytest.xml')], root),
        ('ruff', [sys.executable, '-m', 'ruff', 'check', '--no-cache', 'src', 'tests', 'tools'], root),
        ('contracts', [sys.executable, '-X', 'utf8', 'tools/sync_v3_contracts.py', '--phone', str(args.phone.resolve()), '--check'], root),
        ('frontend-tests', [npm, 'test'], front),
        ('frontend-types', [node, 'node_modules/vue-tsc/bin/vue-tsc.js', '-p', 'tsconfig.v3.json', '--noEmit'], front),
        ('frontend-build', [node, 'node_modules/vite/bin/vite.js', 'build', '--config', 'vite.v3.config.ts', '--outDir', str(output / 'frontend-dist')], front),
    ]
    results = []
    for name, command, cwd in checks:
        print(f'RUN {name}', flush=True)
        started = time.monotonic()
        with (output / f'{name}.stdout.log').open('wb') as stdout, (output / f'{name}.stderr.log').open('wb') as stderr:
            process = subprocess.run(command, cwd=cwd, stdout=stdout, stderr=stderr, check=False,
                                     env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
        results.append({'check': name, 'command': command, 'cwd': str(cwd), 'exit_code': process.returncode,
                        'elapsed_seconds': round(time.monotonic() - started, 3)})
        (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'EXIT {name} {process.returncode} ({results[-1]["elapsed_seconds"]}s)', flush=True)
    print(f'Evidence: {output}', flush=True)
    return int(any(result['exit_code'] != 0 for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
