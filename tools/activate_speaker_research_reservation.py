"""Activate additive prospective metadata, preserving every protected history row."""

import argparse
from pathlib import Path

from short_self_dataset import assets, digest, integrity, read, readonly, write
from allday_asr.v3.adapters.sqlite import V3MigrationRunner
from allday_asr.v3.adapters.sqlite.database import V3Database
from allday_asr.v3.adapters.sqlite.speaker_research_reservations import record_use


def activate(state, previous, output):
    before = read(output / "before/fingerprints.json")
    c = readonly(state)
    assert integrity(c) == before
    backup = output / "core-before-reservation.sqlite3"
    if backup.exists():
        raise ValueError("activation already started; do not repeat migration/backfill")
    import sqlite3

    saved = sqlite3.connect(backup)
    c.backup(saved)
    saved.close()
    c.close()
    schema = V3MigrationRunner(state / "core.sqlite3").initialize()
    original_assets = assets(state)
    assert original_assets == read(output / "before/assets.json")
    database = V3Database(state / "core.sqlite3")
    with database.transaction() as c:
        stamp = c.execute(
            "SELECT activated_at FROM speaker_research_policy"
        ).fetchone()[0]
        snapshot = digest(output / "before/assets.json")
        for source in original_assets["metadata"]["source_files"]:
            c.execute(
                "INSERT OR IGNORE INTO speaker_enrollment_provenance VALUES(?,?,?)",
                (source["sha256"], snapshot, stamp),
            )
        manifest = read(previous / "split-manifest.json")
        manifest_sha = digest(previous / "split-manifest.json")
        for e in manifest["events"]:
            # Historical diagnostic use is metadata, never a new data-role claim.
            record_use(c, e["session_id"], "diagnostic", manifest_sha, stamp)
            if (
                e["was_calibration_positive"]
                or e["was_calibration_negative"]
                or e["calibration_session_exposure"]
            ):
                record_use(c, e["session_id"], "calibration", manifest_sha, stamp)
            if e["was_profile_learning"]:
                record_use(c, e["session_id"], "profile_learning", manifest_sha, stamp)
        assert integrity(c) == before
        policy = dict(c.execute("SELECT * FROM speaker_research_policy").fetchone())
        usage_count = c.execute(
            "SELECT COUNT(*) FROM speaker_research_usage"
        ).fetchone()[0]
        reservations = c.execute(
            "SELECT COUNT(*) FROM session_speaker_reservations"
        ).fetchone()[0]
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    result = {
        "schema_version": schema,
        "policy": policy,
        "historical_rows_unchanged": True,
        "assets_unchanged": True,
        "historical_usage_metadata_rows": usage_count,
        "prospective_reservations": reservations,
        "historical_sessions_reclassified": 0,
        "enrollment_provenance_source_count": len(
            original_assets["metadata"]["source_files"]
        ),
    }
    write(output / "reservation-activation.json", result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.resolve().is_relative_to(a.state_dir.resolve()):
        p.error("backup/diagnostic output must be outside runtime state")
    print(activate(a.state_dir.resolve(), a.previous.resolve(), a.output.resolve()))


if __name__ == "__main__":
    main()
