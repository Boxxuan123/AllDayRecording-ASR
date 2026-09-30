"""Reusable enrollment builder; vectors always come from original clean audio."""

from collections import defaultdict

import numpy as np

from allday_asr.v3.domain.speaker_purity import is_source_eligible_for_clean_profile
from allday_asr.v3.ports.speaker_embeddings import SpeakerClipInput, SpeakerTrackInput


def unit(vector):
    value = np.asarray(vector, dtype=np.float64)
    if value.ndim != 1 or not np.isfinite(value).all() or np.linalg.norm(value) == 0:
        raise ValueError("invalid embedding")
    return value / np.linalg.norm(value)


def build_shadow(sources, provider, *, minimum_quality=0.5):
    """Same longest-five / session-centroid representation as sample worker.

    Audio availability must be established by caller, not inferred from a label.
    The only data read by the provider are eligible raw ranges, never prototypes.
    """
    grouped = defaultdict(list)
    seen = set()
    for source in sources:
        key = (source["target_person_id"], source["source_key"])
        if key in seen:
            continue
        seen.add(key)
        if (
            source.get("source_session_id")
            and source.get('dataset_role') == 'learning'
            and source["end_ms"] - source["start_ms"] >= 800
            and is_source_eligible_for_clean_profile(
                source["evidence"],
                source["target_person_id"],
                audio_available=source.get("audio_available", False),
            )
        ):
            grouped[(source["target_person_id"], source["source_session_id"])].append(
                source
            )
    result = []
    for (person, session), rows in sorted(grouped.items()):
        selected = sorted(
            rows,
            key=lambda r: (
                -(r["end_ms"] - r["start_ms"]),
                r["source_media_id"],
                r["start_ms"],
            ),
        )[:5]
        tracks = tuple(
            SpeakerTrackInput(
                r["source_key"],
                session or "",
                (
                    SpeakerClipInput(
                        r["source_media_id"],
                        r["storage_key"],
                        r["start_ms"],
                        r["end_ms"],
                        None,
                    ),
                ),
            )
            for r in selected
        )
        embeddings = {e.speaker_track_id: e for e in provider.embed(tracks)}
        if any(r["source_key"] not in embeddings for r in selected):
            raise RuntimeError("clean raw source failed embedding; no legacy fallback")
        for row in selected:
            embedding = embeddings[row["source_key"]]
            if (embedding.model, embedding.model_version) != (
                provider.model,
                provider.model_version,
            ):
                raise ValueError("embedding model changed")
            if [
                (c.media_id, c.start_ms, c.end_ms) for c in embedding.representatives
            ] != [(row["source_media_id"], row["start_ms"], row["end_ms"])]:
                raise ValueError("embedding range differs from reviewed source")
        duration_ms = sum(r["end_ms"] - r["start_ms"] for r in selected)
        if duration_ms / 12000 < minimum_quality:
            continue
        result.append(
            {
                "person_id": person,
                "session_id": session,
                "source_keys": [r["source_key"] for r in selected],
                "evidence_ids": [r["evidence"].evidence_id for r in selected],
                "duration_ms": duration_ms,
                "quality": min(1, duration_ms / 12000),
                "vector": unit(
                    np.mean(
                        [unit(embeddings[r["source_key"]].vector) for r in selected],
                        axis=0,
                    )
                ),
            }
        )
    return result
