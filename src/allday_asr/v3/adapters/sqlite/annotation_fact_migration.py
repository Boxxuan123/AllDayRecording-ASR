"""Conservative, repeatable v12 human evidence import. Never order devices by time."""

import json
from datetime import datetime, timezone
from allday_asr.v3.domain.ids import stable_ulid
from .processing_repository_codec import _utterance, _json


def backfill_annotation_facts(connection):
    from .evidence_projection_repository import SqliteEvidenceProjectionRepository

    repo = SqliteEvidenceProjectionRepository(
        connection, now=lambda: datetime.now(timezone.utc).isoformat()
    )
    rows = [_utterance(r) for r in connection.execute("SELECT * FROM utterances")]
    by_id = {r.utterance_id: r for r in rows}

    def person_fact(annotation, row, track=None, identity=None):
        if not annotation or not annotation.get("person_id"):
            return None
        source = annotation.get("source_utterance_id", row.utterance_id)
        revision = annotation.get("source_revision", row.revision)
        fid = stable_ulid(
            "annotation-v13:person:"
            + source
            + ":"
            + str(revision)
            + ":"
            + annotation["person_id"]
        )
        original = by_id.get(source, row)
        refs = annotation.get("audio") or repo.audio_evidence(original)
        payload = {
            **annotation,
            "speaker_track_id": track or row.speaker_track_id,
            "identity": identity or row.identity.value,
        }
        repo.insert_fact(
            fid,
            "person",
            annotation["person_id"],
            original,
            refs,
            payload,
            annotation.get("actor", "legacy-human"),
            annotation.get("confirmed_at", row.updated_at.isoformat()),
        )
        return fid

    deferred_sound = []
    for row in rows:
        ids = dict(row.evidence.get("annotation_fact_ids", {}))
        if ids:
            continue  # already imported or a post-upgrade projection
        corrections = connection.execute(
            """SELECT * FROM correction_operations WHERE target_type='utterance'
            AND target_id=? ORDER BY before_revision,correction_id""",
            (row.utterance_id,),
        ).fetchall()
        sound_id = None
        previous_person = None
        for c in corrections:
            patch = json.loads(c["patch_json"])
            previous = patch.get("previous_values", {})
            if "person_annotation" in patch:
                new = person_fact(
                    patch["person_annotation"], row, patch.get("speaker_track_id")
                )
                old = person_fact(
                    previous.get("person_annotation"),
                    row,
                    previous.get("speaker_track_id"),
                    previous.get("identity"),
                )
                old = old or previous_person
                if not new and old:
                    new = stable_ulid(
                        "annotation-v13:person-revoked:" + c["correction_id"]
                    )
                    repo.insert_fact(
                        new,
                        "person",
                        None,
                        row,
                        repo.audio_evidence(row),
                        {},
                        c["actor"],
                        c["created_at"],
                    )
                if new and old and new != old:
                    repo.supersede(old, new)
                previous_person = new
            if "sound_kind" in patch:
                new = stable_ulid("annotation-v13:sound:" + c["correction_id"])
                repo.insert_fact(
                    new,
                    "sound",
                    patch["sound_kind"],
                    row,
                    repo.audio_evidence(row),
                    {},
                    c["actor"],
                    c["created_at"],
                )
                if sound_id:
                    repo.supersede(sound_id, new)
                sound_id = new
        annotation = row.evidence.get("person_annotation")
        if not annotation and previous_person is None:
            legacy = connection.execute(
                """SELECT l.person_id FROM speaker_tracks t
                JOIN speaker_cluster_memberships m ON m.speaker_track_id=t.speaker_track_id AND m.state='active'
                JOIN person_cluster_links l ON l.cluster_id=m.cluster_id AND l.status='active'
                WHERE t.speaker_track_id=? AND l.source='human' AND (t.label LIKE 'manual:%'
                  OR EXISTS (SELECT 1 FROM correction_operations c WHERE c.target_type='utterance'
                    AND c.target_id=? AND c.actor LIKE 'system:speaker-cluster-identity:%')) """,
                (row.speaker_track_id, row.utterance_id),
            ).fetchall()
            if len(legacy) == 1:
                annotation = {
                    "person_id": legacy[0][0],
                    "source": "legacy_human_selection",
                }
        person_id = person_fact(annotation, row) or previous_person
        if person_id:
            ids["person"] = [person_id]
        if not sound_id and "sound_kind" in row.evidence:
            # Only explicit restoration provenance permits deduplicating a copy.
            annotation = row.evidence.get("person_annotation", {})
            source = by_id.get(annotation.get("restored_from", ""))
            if source:
                deferred_sound.append((row, source.utterance_id))
            else:
                sound_id = stable_ulid(
                    "annotation-v13:legacy-sound:" + row.utterance_id
                )
                repo.insert_fact(
                    sound_id,
                    "sound",
                    row.evidence["sound_kind"],
                    row,
                    repo.audio_evidence(row),
                    {},
                    "legacy-classification",
                    row.updated_at.isoformat(),
                )
        if sound_id:
            ids["sound"] = [sound_id]
        if ids:
            evidence = {**row.evidence, "annotation_fact_ids": ids}
            connection.execute(
                "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
                (_json(evidence), row.utterance_id),
            )
    # A restoration is a projection, never another confirmation. Ambiguous
    # historical versions stay as references; none is promoted by timestamp.
    for row, source_id in deferred_sound:
        matching = [
            r[0]
            for r in connection.execute(
                "SELECT fact_id FROM annotation_facts WHERE dimension='sound' AND source_utterance_id=? AND value_json=?",
                (source_id, _json(row.evidence["sound_kind"])),
            )
        ]
        if not matching:
            source = by_id[source_id]
            fid = stable_ulid(
                "annotation-v13:legacy-sound:"
                + source_id
                + ":"
                + row.evidence["sound_kind"]
            )
            repo.insert_fact(
                fid,
                "sound",
                row.evidence["sound_kind"],
                source,
                repo.audio_evidence(source),
                {},
                "legacy-restoration",
                source.updated_at.isoformat(),
            )
            matching = [fid]
        stored = json.loads(
            connection.execute(
                "SELECT evidence_json FROM utterances WHERE utterance_id=?",
                (row.utterance_id,),
            ).fetchone()[0]
        )
        stored.setdefault("annotation_fact_ids", {})["sound"] = matching
        connection.execute(
            "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
            (_json(stored), row.utterance_id),
        )
    # Competing heads are retained, not resolved by creation time or row revision.
    for f in connection.execute(
        "SELECT fact_id FROM annotation_facts WHERE state IN ('active','conflict')"
    ).fetchall():
        fact = repo.fact(f[0])
        heads = repo.overlapping_facts(fact["audio"], fact["dimension"])
        if any(h["value"] != fact["value"] for h in heads):
            connection.execute(
                "UPDATE annotation_facts SET state='conflict' WHERE fact_id=?", (f[0],)
            )
        repo.revoke_samples(f[0])

    for row in connection.execute("SELECT * FROM utterances").fetchall():
        utterance = _utterance(row)
        projected = repo.annotation_projection_evidence(utterance)
        if projected != utterance.evidence:
            connection.execute(
                "UPDATE utterances SET evidence_json=? WHERE utterance_id=?",
                (_json(projected), utterance.utterance_id),
            )

    from .annotation_fact_migration_effects import publish_migrated_annotations

    publish_migrated_annotations(connection, repo, rows)
