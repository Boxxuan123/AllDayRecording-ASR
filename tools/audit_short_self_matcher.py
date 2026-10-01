"""Private, read-only short self matcher study. Never changes runtime identities.

Run prepare, score development, fit, score/evaluate holdout once, then reference
and report. Dates/sessions freeze before scoring; all predictions are offline JSON.
The tool reuses the real provider/matcher and contains no private event IDs.
"""

import argparse
import json
from pathlib import Path

from short_self_dataset import assets, integrity, prepare, read, readonly, write
from short_self_evidence import score_partition
from short_self_rules import evaluate, fit, summarize
from short_self_regression import replay


def verify(state, output):
    c = readonly(state)
    current = integrity(c)
    c.close()
    same = current == read(output / "before/fingerprints.json")
    same_assets = assets(state) == read(output / "before/assets.json")
    write(
        output / "after/integrity.json",
        {
            "production_identity_blind_profile_tables_unchanged": same,
            "frozen_assets_unchanged": same_assets,
            "protected_table_count": len(current),
            "production_diagnostic_writes": False,
            "production_enabled": False,
        },
    )
    write(output / "after/fingerprints.json", current)
    write(output / "after/assets.json", assets(state))
    if not same or not same_assets:
        raise ValueError("production identity or frozen assets changed")
    return {
        "tables_unchanged": same,
        "assets_unchanged": same_assets,
        "table_count": len(current),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "phase",
        choices=("prepare", "score", "fit", "evaluate", "report", "verify", "regress"),
    )
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--previous-manifest", type=Path)
    p.add_argument("--previous-trace", type=Path)
    p.add_argument("--previous-replay", type=Path)
    p.add_argument("--legacy-db", type=Path)
    p.add_argument("--partition", choices=("development", "holdout", "reference"))
    p.add_argument("--max-per-bin", type=int, default=24)
    args = p.parse_args()
    state, output = args.state_dir.resolve(), args.output.resolve()
    if output.is_relative_to(state):
        p.error("output must be outside runtime state")
    if args.phase == "prepare":
        if args.previous_manifest is None:
            p.error("--previous-manifest required")
        result = prepare(
            state,
            output,
            args.previous_manifest,
            legacy_db=args.legacy_db,
            previous_trace=args.previous_trace,
            max_per_bin=args.max_per_bin,
        )
    elif args.phase == "score":
        if args.partition is None:
            p.error("--partition required")
        result = score_partition(state, output, args.partition)
    elif args.phase == "fit":
        result = fit(output)
    elif args.phase == "evaluate":
        if args.partition is None:
            p.error("--partition required")
        result = evaluate(output, args.partition)
    elif args.phase == "report":
        result = summarize(output)
    elif args.phase == "regress":
        if args.previous_manifest is None or args.previous_replay is None:
            p.error("--previous-manifest and --previous-replay required")
        result = replay(state, output, args.previous_manifest, args.previous_replay)
    else:
        result = verify(state, output)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
