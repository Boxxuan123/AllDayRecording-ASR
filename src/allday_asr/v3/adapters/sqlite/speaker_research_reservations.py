"""Purpose-restricted prospective research provenance, separate from Blind V2."""

import time
from datetime import datetime, timezone


def reservation(connection, session_id):
    row = connection.execute(
        "SELECT * FROM session_speaker_reservations WHERE session_id=?", (session_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def record_prediction(connection, session_id):
    row = reservation(connection, session_id)
    if row is None or row["first_prediction_at"] is not None:
        return row
    reserved = datetime.fromisoformat(row["reserved_at"].replace("Z", "+00:00"))
    # Use actual time; avoid equal millisecond stamps in fast synthetic/real calls.
    while True:
        stamp = (
            datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        if datetime.fromisoformat(stamp.replace("Z", "+00:00")) > reserved:
            break
        time.sleep(0.002)
    connection.execute(
        "UPDATE session_speaker_reservations SET first_prediction_at=? WHERE session_id=? AND first_prediction_at IS NULL",
        (stamp, session_id),
    )
    record_use(connection, session_id, "product_inference", "first-prediction", stamp)
    return reservation(connection, session_id)


def record_use(connection, session_id, purpose, source_id, stamp=None):
    stamp = stamp or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    connection.execute(
        "INSERT OR IGNORE INTO speaker_research_usage VALUES(?,?,?,?)",
        (session_id, purpose, source_id, stamp),
    )


def provenance(connection, utterance_id):
    row = connection.execute(
        "SELECT session_id FROM utterances WHERE utterance_id=?", (utterance_id,)
    ).fetchone()
    if row is None:
        raise KeyError("speaker event does not exist")
    sid = row[0]
    reserved = reservation(connection, sid)
    base = connection.execute(
        "SELECT dataset_role,historical_diagnostic_only FROM session_dataset_roles WHERE session_id=?",
        (sid,),
    ).fetchone()
    uses = {
        r[0]
        for r in connection.execute(
            "SELECT purpose FROM speaker_research_usage WHERE session_id=?", (sid,)
        )
    }
    exposure = bool(
        connection.execute(
            "SELECT 1 FROM session_learning_exposure WHERE session_id=? LIMIT 1", (sid,)
        ).fetchone()
    )
    profile = bool(
        connection.execute(
            "SELECT 1 FROM voice_prototypes p JOIN speaker_tracks t USING(speaker_track_id) WHERE t.session_id=? LIMIT 1",
            (sid,),
        ).fetchone()
    )
    enrollment = bool(
        connection.execute(
            "SELECT 1 FROM purity_candidates WHERE source_session_id=? LIMIT 1", (sid,)
        ).fetchone()
    )
    enrollment = enrollment or bool(
        connection.execute(
            "SELECT 1 FROM capture_segments s JOIN audio_assets a USING(asset_id) "
            "JOIN speaker_enrollment_provenance p ON p.source_sha256=a.sha256 "
            "WHERE s.session_id=? LIMIT 1",
            (sid,),
        ).fetchone()
    )
    flags = {
        "was_enrollment": enrollment or "enrollment" in uses,
        "was_calibration": True
        if "calibration" in uses
        else (False if reserved else None),
        "was_profile_learning": profile or exposure or "profile_learning" in uses,
        "was_development": bool(reserved and reserved["research_role"] == "development")
        or bool(
            uses
            & {
                "development",
                "threshold_fitting",
                "rule_fitting",
                "model_selection",
                "candidate_design",
                "training",
            }
        ),
        "was_blind": base[0] == "blind" or "blind" in uses,
        "was_previous_diagnostic": bool(base[1]) or "diagnostic" in uses,
    }
    independent = bool(
        reserved
        and reserved["research_role"] == "independent_evaluation"
        and not any(flags.values())
    )
    return {
        "session_id": sid,
        "session_role": reserved["research_role"] if reserved else base[0],
        "legacy_dataset_role": base[0],
        "reservation": reserved,
        **flags,
        "is_independent_evaluation": independent,
        "research_uses": sorted(uses),
        "historical_provenance_complete": reserved is not None,
    }
