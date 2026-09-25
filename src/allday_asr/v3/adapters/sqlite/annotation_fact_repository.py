"""Original-audio human facts and their projections; all writes share the UOW."""

import json
from dataclasses import replace
from allday_asr.v3.domain.ids import new_ulid
from allday_asr.v3.domain.identity import SelfIdentity
from .processing_repository_codec import _json


def intersect(a, b):
    return (
        a["media_id"] == b["media_id"]
        and a["start_ms"] < b["end_ms"]
        and b["start_ms"] < a["end_ms"]
    )


def subtract(refs, cuts):
    remaining = [dict(r) for r in refs]
    for cut in cuts:
        result = []
        for r in remaining:
            if not intersect(r, cut):
                result.append(r)
                continue
            if r["start_ms"] < cut["start_ms"]:
                result.append({**r, "end_ms": cut["start_ms"]})
            if r["end_ms"] > cut["end_ms"]:
                result.append({**r, "start_ms": cut["end_ms"]})
        remaining = result
    return remaining


class AnnotationFactMixin:
    def annotation_is_current(self, utterance, dimension):
        ids = utterance.evidence.get("annotation_fact_ids", {}).get(
            dimension, []
        ) + utterance.evidence.get("annotation_review", {}).get("dimensions", {}).get(
            dimension, []
        )
        return all(
            self.fact(fid)["state"] in ("active", "conflict", "revoked") for fid in ids
        )

    def fact(self, fid):
        row = self.connection.execute(
            "SELECT * FROM annotation_facts WHERE fact_id=?", (fid,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown human fact")
        return {
            **dict(row),
            "value": json.loads(row["value_json"]),
            "payload": json.loads(row["payload_json"]),
            "audio": self.fact_audio(fid),
        }

    def fact_audio(self, fid):
        return [
            dict(r)
            for r in self.connection.execute(
                "SELECT media_id,start_ms,end_ms FROM annotation_fact_audio WHERE fact_id=? ORDER BY ordinal",
                (fid,),
            )
        ]

    def overlapping_facts(self, refs, dimension):
        ids = set()
        for r in refs:
            ids.update(
                row[0]
                for row in self.connection.execute(
                    """SELECT DISTINCT f.fact_id
                FROM annotation_facts f JOIN annotation_fact_audio a ON a.fact_id=f.fact_id
                WHERE f.dimension=? AND f.state IN ('active','conflict','revoked') AND a.media_id=?
                AND a.start_ms < ? AND a.end_ms > ?""",
                    (dimension, r["media_id"], r["end_ms"], r["start_ms"]),
                )
            )
        return [self.fact(fid) for fid in sorted(ids)]

    def insert_fact(
        self, fid, dimension, value, before, refs, payload, actor, created_at
    ):
        self.connection.execute(
            """INSERT OR IGNORE INTO annotation_facts
            VALUES (?,?,?,?,?,?,?,'active',?)""",
            (
                fid,
                dimension,
                _json(value),
                before.utterance_id,
                before.revision,
                actor,
                created_at,
                _json(payload),
            ),
        )
        if dimension == "person" and value is None:
            self.connection.execute(
                "UPDATE annotation_facts SET state='revoked' WHERE fact_id=? AND state='active'",
                (fid,),
            )
        self.connection.executemany(
            "INSERT OR IGNORE INTO annotation_fact_audio VALUES (?,?,?,?,?)",
            [
                (fid, i, r["media_id"], r["start_ms"], r["end_ms"])
                for i, r in enumerate(refs)
            ],
        )

    def supersede(self, predecessor, successor):
        self.connection.execute(
            "UPDATE annotation_facts SET state='superseded' WHERE fact_id=?",
            (predecessor,),
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO annotation_supersessions VALUES (?,?)",
            (predecessor, successor),
        )

    def revoke_samples(self, fid):
        f = self.fact(fid)
        if f["dimension"] == "sound" and f["value"] in {"speech", "live_speech"}:
            return
        # Whole multi-clip vectors are withdrawn when any contributing range is tainted.
        incompatible = """COALESCE(p.person_id, (SELECT l.person_id FROM person_cluster_links l
            WHERE l.cluster_id=p.cluster_id AND l.status='active' LIMIT 1), '') != ?"""
        args = [fid, "human_person_revision", fid, f["value"] or ""]
        if f["dimension"] == "sound":
            # v13 backfill runs before the v14 sample-set table exists.
            if not self.connection.execute("SELECT 1 FROM sqlite_master WHERE name='annotation_sample_sets'").fetchone():
                return
            incompatible = """EXISTS (SELECT 1 FROM annotation_sample_sets sample_set
                WHERE sample_set.prototype_id IN (p.prototype_id,p.source_prototype_id))"""
            args = [fid, "human_sound_exclusion", fid]
        self.connection.execute(
            f"""INSERT OR IGNORE INTO annotation_sample_revocations
            SELECT DISTINCT p.prototype_id, ?, ? FROM voice_prototypes p,
            json_each(p.representative_clips_json) clip JOIN annotation_fact_audio a
            ON a.media_id=json_extract(clip.value,'$.media_id')
            AND a.start_ms < json_extract(clip.value,'$.end_ms')
            AND a.end_ms > json_extract(clip.value,'$.start_ms')
            WHERE a.fact_id=? AND ({incompatible})""",
            args,
        )

    def record_annotation_facts(self, before, evidence, dimensions, actor, created_at):
        refs = self.audio_evidence(before)
        if not refs:
            raise ValueError("human annotation requires an original audio mapping")
        result = dict(evidence)
        projected = dict(before.evidence.get("annotation_fact_ids", {}))
        review = dict(before.evidence.get("annotation_review", {}))
        pending = dict(review.get("dimensions", {}))
        affected = set()
        for dimension in dimensions:
            old_ids = list(
                dict.fromkeys(projected.get(dimension, []) + pending.get(dimension, []))
            )
            # Only facts visible in this exact revision are predecessors. Unrelated
            # overlapping confirmations remain competing heads, regardless of time.
            predecessors = [self.fact(fid) for fid in old_ids]
            if any(
                f["state"] not in ("active", "conflict", "revoked")
                for f in predecessors
            ):
                raise ValueError("human fact was superseded; refresh before correcting")
            if dimension == "person":
                annotation = result.get("person_annotation", {})
                value = annotation.get("person_id")
                payload = {
                    **annotation,
                    "speaker_track_id": result.pop("_annotation_track"),
                    "identity": result.pop("_annotation_identity"),
                }
            else:
                value = result.get("sound_kind", "speech")
                payload = {}
            fid = new_ulid()
            self.insert_fact(
                fid, dimension, value, before, refs, payload, actor, created_at
            )
            for old in predecessors:
                self.supersede(old["fact_id"], fid)
                # A correction to a split projection preserves the untouched ranges.
                remainder = subtract(old["audio"], refs)
                if remainder:
                    residual = new_ulid()
                    self.insert_fact(
                        residual,
                        dimension,
                        old["value"],
                        before,
                        remainder,
                        old["payload"],
                        old["actor"],
                        old["created_at"],
                    )
                    self.connection.execute(
                        "INSERT INTO annotation_supersessions VALUES (?,?)",
                        (old["fact_id"], residual),
                    )
            projected[dimension] = [fid]
            pending.pop(dimension, None)
            heads = self.overlapping_facts(refs, dimension)
            if len({_json(f["value"]) for f in heads}) > 1:
                for f in heads:
                    self.connection.execute(
                        "UPDATE annotation_facts SET state='conflict' WHERE fact_id=?",
                        (f["fact_id"],),
                    )
                pending[dimension] = [f["fact_id"] for f in heads]
            self.revoke_samples(fid)
            # Cascade from historical projections too, not just the new utterance.
            related_ids = sorted(set(old_ids) | {f["fact_id"] for f in heads})
            # Filter in SQLite rather than decoding every transcript for every
            # item of a phone batch. Include historical projections as before.
            affected.update(
                row[0] for row in self.connection.execute(
                    """SELECT u.utterance_id FROM utterances u
                    WHERE EXISTS (
                        SELECT 1 FROM json_each(u.evidence_json, ?) fact
                        WHERE fact.value IN (SELECT value FROM json_each(?))
                    )""",
                    (f"$.annotation_fact_ids.{dimension}", _json(related_ids)),
                )
            )
        result["annotation_fact_ids"] = projected
        result.pop("annotation_outdated", None)
        if pending:
            result["annotation_review"] = {
                "reason": "ambiguous_audio_mapping",
                "dimensions": pending,
                "candidates": [
                    self.fact(fid)["payload"] for ids in pending.values() for fid in ids
                ],
            }
        else:
            result.pop("annotation_review", None)
        return self.annotation_projection_evidence(
            replace(before, evidence=result)
        ), sorted(affected)

    def annotation_projection_evidence(self, utterance):
        evidence = dict(utterance.evidence)
        outdated = []
        pending = dict(evidence.get("annotation_review", {}).get("dimensions", {}))
        for dimension, ids in evidence.get("annotation_fact_ids", {}).items():
            states = [self.fact(fid)["state"] for fid in ids]
            if "superseded" in states:
                outdated.append(dimension)
                pending.pop(dimension, None)
            elif "conflict" in states:
                pending[dimension] = [
                    f["fact_id"]
                    for f in self.overlapping_facts(
                        self.audio_evidence(utterance), dimension
                    )
                ]
        if outdated:
            evidence["annotation_outdated"] = outdated
        else:
            evidence.pop("annotation_outdated", None)
        if pending:
            evidence["annotation_review"] = {
                "reason": "ambiguous_audio_mapping",
                "dimensions": pending,
                "candidates": [
                    self.fact(fid)["payload"] for ids in pending.values() for fid in ids
                ],
            }
        else:
            evidence.pop("annotation_review", None)
        return evidence

    def _restore_annotation(self, utterance):
        refs = self.audio_evidence(utterance)
        evidence = dict(utterance.evidence)
        projected, pending = {}, {}
        track, identity = utterance.speaker_track_id, utterance.identity
        for dimension in ("person", "sound"):
            facts = self.overlapping_facts(refs, dimension)
            orphan_ids = self.connection.execute(
                """SELECT f.fact_id FROM annotation_facts f
                JOIN utterances old ON old.utterance_id=f.source_utterance_id
                WHERE f.dimension=? AND f.state IN ('active','conflict','revoked')
                  AND old.session_id=? AND old.start_ms<? AND old.end_ms>?
                  AND NOT EXISTS (SELECT 1 FROM annotation_fact_audio a WHERE a.fact_id=f.fact_id)""",
                (dimension, utterance.session_id, utterance.end_ms, utterance.start_ms),
            ).fetchall()
            facts.extend(self.fact(row[0]) for row in orphan_ids)
            if not facts:
                continue
            same = len({_json(f["value"]) for f in facts}) == 1
            covered = (
                bool(refs)
                and all(f["audio"] for f in facts)
                and not subtract(refs, [r for f in facts for r in f["audio"]])
            )
            # Person splits remain reviewable; sound can restore fully covered splits/merges.
            exact = all(
                not subtract(f["audio"], refs) and not subtract(refs, f["audio"])
                for f in facts
            )
            if dimension == "person" and exact:
                exact = all(
                    not f["value"]
                    or self.connection.execute(
                        "SELECT 1 FROM speaker_tracks WHERE speaker_track_id=? AND session_id=?",
                        (f["payload"].get("speaker_track_id"), utterance.session_id),
                    ).fetchone()
                    for f in facts
                )
            if same and covered and (dimension == "sound" or exact):
                f = facts[0]
                projected[dimension] = [v["fact_id"] for v in facts]
                if dimension == "sound":
                    evidence["sound_kind"] = f["value"]
                elif f["value"]:
                    evidence["person_annotation"] = {
                        **f["payload"],
                        "fact_id": f["fact_id"],
                        "person_id": f["value"],
                        "audio": f["audio"],
                        "restored_from": f["source_utterance_id"],
                    }
                    track = f["payload"]["speaker_track_id"]
                    identity = SelfIdentity(f["payload"]["identity"])
            else:
                pending[dimension] = [f["fact_id"] for f in facts]
        if projected:
            evidence["annotation_fact_ids"] = projected
        if pending:
            evidence["annotation_review"] = {
                "reason": "ambiguous_audio_mapping",
                "dimensions": pending,
                "candidates": [
                    self.fact(fid)["payload"] for ids in pending.values() for fid in ids
                ],
            }
        return replace(
            utterance, evidence=evidence, speaker_track_id=track, identity=identity
        )
