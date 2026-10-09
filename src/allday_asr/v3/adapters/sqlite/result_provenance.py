"""Read existing stage metadata and generation snapshots without fabricating history."""
import json


class ResultProvenanceRepository:
    def __init__(self, database):
        self.database = database

    def get(self, kind, identifier):
        if kind == "generation":
            query = "SELECT input_scope_json AS data FROM generation_records WHERE generation_id=?"
        elif kind == "artifact":
            query = "SELECT metadata_json AS data FROM artifacts WHERE artifact_id=?"
        elif kind == "speaker-run":
            query = "SELECT policy_json AS data FROM speaker_cluster_runs WHERE cluster_run_id=?"
        elif kind == "identity-match":
            query = "SELECT details_json AS data FROM audit_entries WHERE target_type=\'identity_match_snapshot\' AND target_id=?"
        else:
            raise ValueError("provenance kind must be generation, artifact, speaker-run or identity-match")
        with self.database.read() as c:
            row = c.execute(query, (identifier,)).fetchone()
        if row is None:
            raise LookupError(identifier)
        data = json.loads(row["data"])
        provenance = data.get("provenance")
        return {"kind": kind, "id": identifier,
                "status": "recorded" if provenance is not None else "historical_unknown",
                "provenance": provenance, "existing_source_data": data}
