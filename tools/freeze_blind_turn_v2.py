"""Explicit local V2 activation; copies the already frozen V1 snapshot exactly."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from allday_asr.v3.adapters.blind_validation import BlindValidationService  # noqa: E402
from allday_asr.v3.adapters.sqlite.database import V3Database  # noqa: E402


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--experiment", required=True)
    args = parser.parse_args()

    def verify(snapshot):
        for name, expected in snapshot["model_files"].items():
            path = (args.model_root / name).resolve()
            if not path.is_relative_to(args.model_root.resolve()):
                raise ValueError("model path escaped root")
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("frozen model changed")

    database = V3Database.open(args.database)
    service = BlindValidationService(database, None, args.output / "reports", model_verifier=verify)
    result = service.upgrade_turn_experiment(args.experiment)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "blind-v2-snapshot.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("experiment_id", "snapshot_hash", "clean_profile_version")}))


if __name__ == "__main__":
    main()
