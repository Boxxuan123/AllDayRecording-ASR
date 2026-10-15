"""Real synthetic ASR source change through the actual Phone SQLite consumer."""
import argparse
import json
import os
import subprocess
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phone', required=True, type=Path)
    parser.add_argument('--node', required=True, type=Path, help='Node >=22 with node:sqlite')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from tests.test_v32_three_layer_knowledge import V32ThreeLayerKnowledgeTests, UTTERANCE_ID
    from allday_asr.v3.bootstrap import compose_v3_core, V3CorePaths
    from allday_asr.v3.config import CodexReminderSettings
    from allday_asr.v3.application import CorrectUtteranceCommand
    from allday_asr.v3.domain import SyncRequest
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    seed = V32ThreeLayerKnowledgeTests()
    seed.setUp()
    core = compose_v3_core(V3CorePaths.from_state_dir(seed.root), codex_settings=CodexReminderSettings(enabled=False))
    try:
        published = core.insights.refresh_daily('2026-09-01')
        initial = core.mobile_sync.synchronize('computer-1', SyncRequest(5, 'cursor-0', (), 100))
        core.corrections.correct_utterance(CorrectUtteranceCommand(
            utterance_id=UTTERANCE_ID, expected_revision=1, text='修订后的合成材料', actor='synthetic-test'))
        core.daily_automation.catch_up()
        after = core.mobile_sync.synchronize('computer-1', SyncRequest(5, initial.next_cursor, (), 100))
        bootstrap = core.mobile_sync.synchronize('computer-1', SyncRequest(5, 'cursor-0', (), 100, True))
        payload = {'summary_id': published['summary_id'], 'revision': published['revision'],
                   'objective': {k: v for k, v in published['objective'].items() if k != 'events'},
                   'responses': [initial.as_dict(), after.as_dict()], 'bootstrap': bootstrap.as_dict()}
        fixture = output / 'server-responses.json'
        fixture.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        command = [str(args.node.resolve()), str(args.phone.resolve() / 'tools/quality/check-daily-sync-stale.cjs'),
                   str(fixture), str(output / 'phone.sqlite3')]
        with (output / 'phone.stdout.log').open('wb') as stdout, (output / 'phone.stderr.log').open('wb') as stderr:
            result = subprocess.run(command, check=False, stdout=stdout, stderr=stderr,
                env={**os.environ, 'PHONE_TEST_TYPESCRIPT': str(root / 'all_day_recording_front/node_modules/typescript')})
        (output / 'results.json').write_text(json.dumps({'command': command, 'exit_code': result.returncode,
            'scope': 'real synthetic composition/correction/coordinator/sync and actual Phone SQLite consumer; OS APIs mocked'}, indent=2), encoding='utf-8')
        print((output / 'phone.stdout.log').read_text(encoding='utf-8'))
        return result.returncode
    finally:
        core.close()
        seed.tearDown()


if __name__ == '__main__':
    raise SystemExit(main())
