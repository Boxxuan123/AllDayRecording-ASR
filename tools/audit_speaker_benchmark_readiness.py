"""Read-only readiness; bind future manifests to actual reservation/human facts.

No model is loaded, no prediction is written, and historical truth is not exported.
Use --manifest only for a prospectively reserved study, before freezing its plan.
"""

import argparse
import json
from pathlib import Path

from short_self_dataset import integrity, read, readonly, write
from speaker_benchmark_protocol import (
    FORBIDDEN_FLAGS,
    PROTOCOL_VERSION,
    independent_gate,
)
from allday_asr.v3.adapters.sqlite.speaker_research_reservations import provenance


MANIFEST_CONTRACT = {
    "protocol": PROTOCOL_VERSION,
    "unit": "one clean single-speaker event; overlapping crops never separate events",
    "required_fields": [
        "event_id",
        "utterance_id",
        "session_id",
        "date",
        "session_role",
        "reservation",
        "duration_ms",
        "source_windows",
        "normalized_audio_sha256",
        "truth",
        "truth_fact_ids",
        "truth_reviewed_at",
        "purity",
        "mapping_complete",
        "has_overlap",
        "exclusions",
        *FORBIDDEN_FLAGS,
    ],
    "source_window_fields": ["media_id", "sha256", "storage_key", "start_ms", "end_ms"],
    "audio": "existing provider preprocessing; mono float32 16000 Hz; exact valid length",
    "selection": "all eligible prospective events in a predeclared date range; report every exclusion",
    "split": "reserved session AND UTC date; development and evaluation disjoint",
    "truth": "active human person fact plus independently reviewed clean-single source purity",
    "freeze": "hash source bytes, normalized bytes, facts, split, exclusions, model/ref/threshold plan before evaluation",
    "evaluation": "one study attempt; failed evaluation also consumes study; never fit/rank/select using evaluation",
    "production_enabled": False,
}


def bind_database_events(connection, events):
    """Reject self-declared clean provenance and truth not backed by this database."""
    self_id = connection.execute(
        "SELECT person_id FROM persons WHERE kind='self'"
    ).fetchone()[0]
    for e in events:
        actual = provenance(connection, e["utterance_id"])
        if any(
            e.get(k) != actual[k]
            for k in ("session_id", "session_role", "reservation", *FORBIDDEN_FLAGS)
        ):
            raise ValueError("manifest reservation/provenance differs from database")
        facts = []
        for fid in e["truth_fact_ids"]:
            f = connection.execute(
                "SELECT * FROM annotation_facts WHERE fact_id=?", (fid,)
            ).fetchone()
            if (
                f is None
                or f["state"] != "active"
                or f["dimension"] != "person"
                or f["actor"].startswith("system:")
                or f["source_utterance_id"] != e["utterance_id"]
            ):
                raise ValueError("active human event truth required")
            value = json.loads(f["value_json"])
            if (
                not isinstance(value, str)
                or not value
                or (value == self_id) != (e["truth"] == "self")
            ):
                raise ValueError("manifest truth differs from human fact")
            facts.append(dict(f))
        if not facts or e["truth_reviewed_at"] != max(f["created_at"] for f in facts):
            raise ValueError("truth timestamp differs from human fact")
        for w in e["source_windows"]:
            available = connection.execute(
                "SELECT 1 FROM capture_segments s JOIN audio_assets a USING(asset_id) "
                "JOIN audio_replicas r USING(replica_id) WHERE s.session_id=? AND a.media_id=? "
                "AND a.sha256=? AND r.storage_key=? AND r.state='available' "
                "AND s.source_start_ms<=? AND s.source_end_ms>=?",
                (
                    e["session_id"],
                    w["media_id"],
                    w["sha256"],
                    w["storage_key"],
                    w["start_ms"],
                    w["end_ms"],
                ),
            ).fetchone()
            anchors = [
                a
                for f in facts
                for a in connection.execute(
                    "SELECT * FROM annotation_fact_audio WHERE fact_id=?",
                    (f["fact_id"],),
                )
            ]
            if not available or not any(
                a["media_id"] == w["media_id"]
                and a["start_ms"] <= w["start_ms"]
                and a["end_ms"] >= w["end_ms"]
                for a in anchors
            ):
                raise ValueError(
                    "source mapping lacks available original audio/human anchor"
                )
            purity = connection.execute(
                "SELECT e.purity,e.conflicting,e.review_primary_person_id FROM speaker_purity_sources s JOIN speaker_purity_current c USING(source_key) "
                "JOIN speaker_source_purity_evidence e USING(evidence_id) WHERE s.source_media_id=? "
                "AND s.start_ms<=? AND s.end_ms>=?",
                (w["media_id"], w["start_ms"], w["end_ms"]),
            ).fetchall()
            people = {json.loads(f["value_json"]) for f in facts}
            if (
                len(people) != 1
                or not purity
                or any(
                    r[0] != "clean_single" or r[1] or r[2] not in people for r in purity
                )
            ):
                raise ValueError("reviewed clean-single source purity required")
    return events


def audit(state, output, manifest=None):
    c = readonly(state)
    before = integrity(c)
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM session_speaker_reservations ORDER BY capture_date_utc,session_id"
        )
    ]
    events = bind_database_events(c, read(manifest)) if manifest else []
    result = {
        "protocol": PROTOCOL_VERSION,
        "reservations": rows,
        "gate": independent_gate(events),
        "manifest_provided": manifest is not None,
        "models_compared": [],
        "status": "NO MODEL BENCHMARK RUN YET",
        "protected_history_unchanged": integrity(c) == before,
    }
    c.close()
    write(output / "manifest-contract.json", MANIFEST_CONTRACT)
    write(output / "readiness.json", result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--manifest", type=Path)
    a = p.parse_args()
    if a.output.resolve().is_relative_to(a.state_dir.resolve()):
        p.error("private benchmark evidence must stay outside runtime state")
    print(json.dumps(audit(a.state_dir, a.output, a.manifest), ensure_ascii=False))


if __name__ == "__main__":
    main()
