"""Persisted session reservation controls; no per-upload manual selection."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from allday_asr.v3.bootstrap import compose_v3_core  # noqa: E402
from allday_asr.v3.adapters.sqlite.dataset_reservations import configure, open_holdout  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'enable-collection', 'disable-collection', 'configure', 'open-holdout'])
    parser.add_argument('--policy-version')
    parser.add_argument('--blind-ratio', type=float)
    parser.add_argument('--holdout-ratio', type=float)
    parser.add_argument('--session')
    parser.add_argument('--decision-version')
    parser.add_argument('--reason')
    args = parser.parse_args()
    core = compose_v3_core()
    core.initialize()
    try:
        if args.command in {'enable-collection', 'disable-collection', 'configure'}:
            with core.database.transaction() as c:
                configure(c, actor='local-cli', collection=args.command == 'enable-collection' if args.command != 'configure' else None,
                          blind_ratio=args.blind_ratio, holdout_ratio=args.holdout_ratio, policy_version=args.policy_version)
        elif args.command == 'open-holdout':
            if not args.session or not args.decision_version or not args.reason:
                parser.error('open-holdout requires --session --decision-version --reason')
            with core.database.transaction() as c:
                open_holdout(c, args.session, args.decision_version, 'local-cli', args.reason)
        print(json.dumps(core.blind_validation.status(), ensure_ascii=False, indent=2))
        core.blind_validation.report()
    finally:
        core.close()


if __name__ == '__main__':
    main()
