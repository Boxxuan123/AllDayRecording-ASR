"""Audited append-only recovery of one prematurely ingested Phone recording.

Run without --apply for a read-only preview. This tool does not replace the
original received manifest or its immutable Core row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import V3Database
from allday_asr.v3.domain.ids import stable_ulid
from allday_asr.v3.domain.models import (
    AudioAsset,
    AudioFormat,
    AudioReplica,
    AudioReplicaState,
    CaptureSegment,
    ChangeOperation,
)
from allday_asr.v3.adapters.sqlite.unit_of_work import SqliteUnitOfWork
from allday_asr.v3.application.durable_processing_support import _session_projection

SESSION_ID = "69WRQ94VSEQVX0TPXN0ASQSYHC"
DIRECTORY = "pcm_gap_test_1790503677981"
EXPECTED_SAMPLES = 67_641_280
EXPECTED_CHUNKS = 71
NAME = re.compile(r"segment_(\d{6})_first_(\d{12})\.wav")


def require(condition, message):
    # Recovery validation and catalog insertions must also run under python -O.
    if not condition:
        raise ValueError(message)


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            hasher.update(block)
    return hasher.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    root = args.root.resolve()
    source = root / "data" / "phone-inbox" / DIRECTORY
    state = root / "state" / "v3"
    database = V3Database(state / "core.sqlite3")
    old_path = source / "session_summary.json"
    old_bytes = old_path.read_bytes()
    old = json.loads(old_bytes)
    files = sorted(source.glob("segment_*.wav"))
    require(
        len(files) == EXPECTED_CHUNKS,
        "repair precondition failed: len(files) == EXPECTED_CHUNKS",
    )
    records = {}
    for path in (root / "data" / "phone-inbox" / ".uploads").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("relative_path", "").startswith(DIRECTORY + "/"):
            records[record["relative_path"]] = record
    chunks = []
    hashes = []
    next_sample = 0
    for number, path in enumerate(files, 1):
        match = NAME.fullmatch(path.name)
        require(
            match and int(match[1]) == number and int(match[2]) == next_sample,
            "repair precondition failed: match and int(match[1]) == number and int(match[2]) == next_sample",
        )
        with wave.open(str(path), "rb") as wav:
            require(
                (
                    wav.getframerate(),
                    wav.getnchannels(),
                    wav.getsampwidth(),
                    wav.getcomptype(),
                )
                == (16000, 1, 2, "NONE"),
                'repair precondition failed: (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) == (16000, 1, 2, "NONE")',
            )
            count = wav.getnframes()
        require(
            path.stat().st_size == 44 + count * 2,
            "repair precondition failed: path.stat().st_size == 44 + count * 2",
        )
        sha = digest(path)
        receipt = records.get(f"{DIRECTORY}/{path.name}")
        require(
            receipt and receipt["status"] == "completed",
            'repair precondition failed: receipt and receipt["status"] == "completed"',
        )
        require(
            receipt["sha256"] == sha and receipt["size"] == path.stat().st_size,
            'repair precondition failed: receipt["sha256"] == sha and receipt["size"] == path.stat().st_size',
        )
        chunks.append(
            {
                "index": number,
                "fileName": path.name,
                "firstSample": next_sample,
                "sampleCount": count,
            }
        )
        hashes.append(sha)
        next_sample += count
    require(
        next_sample == EXPECTED_SAMPLES,
        "repair precondition failed: next_sample == EXPECTED_SAMPLES",
    )
    require(
        old["chunks"] == chunks[:1] and old["totalSamples"] == chunks[0]["sampleCount"],
        'repair precondition failed: old["chunks"] == chunks[:1] and old["totalSamples"] == chunks[0]["sampleCount"]',
    )
    full = {
        **old,
        "chunks": chunks,
        "completedSegments": len(chunks),
        "totalSamples": next_sample,
        "continuityValid": True,
    }
    full_bytes = (json.dumps(full, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    full_sha = hashlib.sha256(full_bytes).hexdigest()
    with database.read() as connection:
        session = connection.execute(
            "SELECT * FROM recording_sessions WHERE session_id=?", (SESSION_ID,)
        ).fetchone()
        original = connection.execute(
            "SELECT * FROM session_manifests WHERE session_id=?", (SESSION_ID,)
        ).fetchone()
        segments = connection.execute(
            "SELECT * FROM capture_segments WHERE session_id=? ORDER BY sequence",
            (SESSION_ID,),
        ).fetchall()
        has_versions = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_manifest_revisions'"
        ).fetchone()
        versions = (
            connection.execute(
                "SELECT * FROM session_manifest_revisions WHERE session_id=? ORDER BY input_revision",
                (SESSION_ID,),
            ).fetchall()
            if has_versions
            else []
        )
        old_runs = connection.execute(
            "SELECT run_id,status,input_revision FROM processing_runs WHERE session_id=?",
            (SESSION_ID,),
        ).fetchall()
        edits = connection.execute(
            "SELECT COUNT(*) FROM correction_operations WHERE target_id IN (SELECT utterance_id FROM utterances WHERE session_id=?)",
            (SESSION_ID,),
        ).fetchone()[0]
    require(
        session and original and session["legacy_ref"].endswith(old["sessionKey"]),
        'repair precondition failed: session and original and session["legacy_ref"].endswith(old["sessionKey"])',
    )
    require(
        original["sha256"] == hashlib.sha256(old_bytes).hexdigest(),
        'repair precondition failed: original["sha256"] == hashlib.sha256(old_bytes).hexdigest()',
    )
    require(
        len(segments) in (1, EXPECTED_CHUNKS),
        "repair precondition failed: len(segments) in (1, EXPECTED_CHUNKS)",
    )
    require(
        segments[0]["start_sample"] == 0 and segments[0]["asset_id"],
        'repair precondition failed: segments[0]["start_sample"] == 0 and segments[0]["asset_id"]',
    )
    if versions:
        require(
            len(versions) == 1
            and versions[0]["sha256"] == full_sha
            and len(segments) == EXPECTED_CHUNKS,
            'repair precondition failed: len(versions) == 1 and versions[0]["sha256"] == full_sha and len(segments) == EXPECTED_CHUNKS',
        )
    else:
        require(len(segments) == 1, "repair precondition failed: len(segments) == 1")
    summary = {
        "session_id": SESSION_ID,
        "old_manifest_sha256": original["sha256"],
        "full_manifest_sha256": full_sha,
        "chunks": len(chunks),
        "total_samples": next_sample,
        "duration_ms": next_sample // 16,
        "new_segments": max(0, EXPECTED_CHUNKS - len(segments)),
        "old_runs": [dict(row) for row in old_runs],
        "correction_operations": edits,
        "already_applied": bool(versions),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not args.apply or versions:
        return
    database.initialize()
    # Publish immutable content before the database transaction. Orphaned
    # content is harmless if the transaction fails; no DB row points to it.
    manifest_path = source / "session_summary.revision-2.json"
    if manifest_path.exists():
        require(
            manifest_path.read_bytes() == full_bytes,
            "repair precondition failed: manifest_path.read_bytes() == full_bytes",
        )
    else:
        manifest_path.write_bytes(full_bytes)
    audio_store = ContentAddressedStore(state / "audio")
    artifact_store = ContentAddressedStore(state / "artifacts")
    stored_manifest = artifact_store.put_file(manifest_path, expected_sha256=full_sha)
    stored_audio = [
        audio_store.put_file(path, expected_sha256=sha)
        for path, sha in zip(files[1:], hashes[1:], strict=True)
    ]
    now = datetime.now(timezone.utc)
    start = datetime.fromtimestamp(old["sessionStartedAt"] / 1000, tz=timezone.utc)
    with SqliteUnitOfWork(database) as uow:
        current = uow.catalog.get_session(SESSION_ID)
        require(
            current.revision == session["revision"],
            'repair precondition failed: current.revision == session["revision"]',
        )
        require(
            uow.catalog.connection.execute(
                "SELECT COUNT(*) FROM capture_segments WHERE session_id=?",
                (SESSION_ID,),
            ).fetchone()[0]
            == 1,
            'repair precondition failed: uow.catalog.connection.execute("SELECT COUNT(*) FROM capture_segments WHERE session_id=?", (SESSION_ID,)).fetc',
        )
        require(
            not uow.catalog.connection.execute(
                "SELECT 1 FROM session_manifest_revisions WHERE session_id=?",
                (SESSION_ID,),
            ).fetchone(),
            'repair precondition failed: not uow.catalog.connection.execute("SELECT 1 FROM session_manifest_revisions WHERE session_id=?", (SESSION_ID,',
        )
        old_replica = uow.catalog.connection.execute(
            "SELECT device_id FROM audio_replicas WHERE replica_id=?",
            (segments[0]["replica_id"],),
        ).fetchone()
        device_id = old_replica[0]
        for offset, (chunk, sha, stored) in enumerate(
            zip(chunks[1:], hashes[1:], stored_audio, strict=True), 1
        ):
            size = files[offset].stat().st_size
            duration = round(chunk["sampleCount"] / 16)
            asset = AudioAsset(
                asset_id=stable_ulid("audio-asset", sha),
                sha256=sha,
                size_bytes=size,
                duration_ms=duration,
                format=AudioFormat.WAV,
                media_id=stored.media_id,
                created_at=now,
                legacy_ref=None,
            )
            existing = uow.catalog.find_asset_by_sha256(sha)
            if existing:
                require(
                    (existing.size_bytes, existing.duration_ms, existing.media_id)
                    == (size, duration, stored.media_id),
                    "repair precondition failed: (existing.size_bytes, existing.duration_ms, existing.media_id) == (size, duration, stored.media_id)",
                )
            else:
                require(
                    uow.catalog.add_asset(asset),
                    "repair precondition failed: uow.catalog.add_asset(asset)",
                )
            asset = existing or asset
            replica_id = stable_ulid(
                "phone-replica", device_id, SESSION_ID, chunk["index"]
            )
            require(
                uow.catalog.add_replica(
                    AudioReplica(
                        replica_id=replica_id,
                        asset_id=asset.asset_id,
                        device_id=device_id,
                        storage_key=stored.storage_key,
                        state=AudioReplicaState.AVAILABLE,
                        verified_at=now,
                        created_at=now,
                        legacy_ref=f"{session['legacy_ref']}:chunk:{chunk['index']}",
                    )
                ),
                "repair precondition failed: uow.catalog.add_replica(AudioReplica(replica_id=replica_id, asset_id=asset.asset_id,                 device_id",
            )
            start_ms = round(chunk["firstSample"] / 16)
            end_ms = round((chunk["firstSample"] + chunk["sampleCount"]) / 16)
            require(
                uow.catalog.add_segment(
                    CaptureSegment(
                        segment_id=stable_ulid(
                            "capture-segment", SESSION_ID, chunk["index"]
                        ),
                        session_id=SESSION_ID,
                        asset_id=asset.asset_id,
                        replica_id=replica_id,
                        sequence=offset,
                        session_start_ms=start_ms,
                        session_end_ms=end_ms,
                        source_start_ms=0,
                        source_end_ms=duration,
                        start_sample=chunk["firstSample"],
                        captured_at=start + timedelta(milliseconds=start_ms),
                        legacy_ref=f"{session['legacy_ref']}:segment:{chunk['index']}",
                    )
                ),
                'repair precondition failed: uow.catalog.add_segment(CaptureSegment(                 segment_id=stable_ulid("capture-segment", SESSION_ID, ',
            )
            uow.changes.append(
                "audio_asset",
                asset.asset_id,
                1,
                ChangeOperation.UPSERT.value,
                {
                    "asset_id": asset.asset_id,
                    "session_id": SESSION_ID,
                    "sequence": offset,
                    "sha256": sha,
                    "size_bytes": size,
                    "duration_ms": duration,
                    "format": "wav",
                },
            )
        uow.catalog.connection.execute(
            "INSERT INTO session_manifest_revisions "
            "(session_id,input_revision,sha256,storage_ref,entries_json,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                SESSION_ID,
                2,
                full_sha,
                stored_manifest.storage_key,
                json.dumps(
                    full, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                "append 70 verified files after premature one-minute manifest",
                now.isoformat(),
            ),
        )
        end = start + timedelta(milliseconds=next_sample // 16)
        revision = current.revision + 1
        uow.catalog.connection.execute(
            "UPDATE recording_sessions SET captured_end=?, revision=?, "
            "state='admission_pending',status_code='backup_required',current_stage='backup_admission', "
            "progress=0,blocking_reason='backup_restore_evidence_required',updated_at=? WHERE session_id=?",
            (
                end.isoformat().replace("+00:00", "Z"),
                revision,
                now.isoformat().replace("+00:00", "Z"),
                SESSION_ID,
            ),
        )
        refreshed = uow.catalog.get_session(SESSION_ID)
        uow.changes.append(
            "recording_session",
            SESSION_ID,
            revision,
            ChangeOperation.UPSERT.value,
            {
                **_session_projection(refreshed),
                "session_key": old["sessionKey"],
                "input_revision": 2,
                "manifest_sha256": full_sha,
                "segment_count": EXPECTED_CHUNKS,
                "total_samples": EXPECTED_SAMPLES,
            },
        )
        uow.audit.append(
            "phone.session.append_repair",
            "system:controlled_repair",
            "recording_session",
            SESSION_ID,
            {
                **summary,
                "input_revision": 2,
                "new_manifest_storage_ref": stored_manifest.storage_key,
            },
        )
    print("APPLIED input_revision=2")


if __name__ == "__main__":
    main()
