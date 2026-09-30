"""Administrative changes are versioned; reservations are created by SQL trigger."""

import json
from datetime import datetime, timezone


def now():
    return datetime.now(timezone.utc).isoformat()


def settings(connection):
    return dict(connection.execute("SELECT * FROM dataset_reservation_settings WHERE singleton=1").fetchone())


def configure(connection, *, actor, collection=None, blind_ratio=None, holdout_ratio=None, policy_version=None):
    current = settings(connection)
    if collection is not None and type(collection) is not bool:
        raise ValueError("collection mode must be boolean")
    values = {
        "blind_ratio": current["blind_ratio"] if blind_ratio is None else float(blind_ratio),
        "holdout_ratio": current["holdout_ratio"] if holdout_ratio is None else float(holdout_ratio),
        "blind_collection_mode": current["blind_collection_mode"] if collection is None else int(collection),
        "policy_version": policy_version or current["policy_version"],
    }
    if not (0 <= values["blind_ratio"] <= 1 and 0 <= values["holdout_ratio"] <= 1
            and values["blind_ratio"] + values["holdout_ratio"] <= 1):
        raise ValueError("reservation ratios must be nonnegative and total at most one")
    if (blind_ratio is not None or holdout_ratio is not None) and (
        not policy_version or policy_version == current["policy_version"]
    ):
        raise ValueError("ratio changes require a new policy version")
    stamp = now()
    connection.execute("""UPDATE dataset_reservation_settings SET policy_version=?,blind_ratio=?,
        holdout_ratio=?,blind_collection_mode=?,revision=revision+1,updated_at=? WHERE singleton=1""",
        (values["policy_version"], values["blind_ratio"], values["holdout_ratio"],
         values["blind_collection_mode"], stamp))
    result = settings(connection)
    connection.execute("INSERT INTO dataset_setting_audit VALUES(?,?,?,?)",
                       (result["revision"], json.dumps(result), actor, stamp))
    return result


def open_holdout(connection, session_id, version, actor, reason):
    if not version.strip() or not reason.strip():
        raise ValueError("holdout opening requires decision version and reason")
    row = connection.execute("SELECT dataset_role FROM session_dataset_roles WHERE session_id=?", (session_id,)).fetchone()
    if row is None or row[0] != "holdout":
        raise ValueError("session is not a holdout")
    # Permission for a named future study, never admission to learning/current blind.
    connection.execute("INSERT INTO holdout_openings VALUES(?,?,?,?,?)", (session_id, version, actor, reason, now()))
    return {"session_id": session_id, "decision_version": version, "dataset_role": "holdout"}
