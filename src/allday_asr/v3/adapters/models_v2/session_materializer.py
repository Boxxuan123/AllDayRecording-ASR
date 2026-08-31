from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from allday_asr.services.session_ingest import ingest_session_manifest
from allday_asr.storage.database import Database
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.sqlite import V3Database


_LEGACY_SESSION = re.compile(r"^v2:.*:recording_sessions:([1-9][0-9]*)$")
_SAFE_SESSION_DIRECTORY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class V2SessionMaterializer:
    """Create an internal V2-compatible manifest view for a native V3 session."""

    def __init__(
        self,
        database: V3Database,
        audio_store: ContentAddressedStore,
        artifact_store: ContentAddressedStore,
        root: Path,
    ) -> None:
        self.database = database
        self.audio_store = audio_store
        self.artifact_store = artifact_store
        self.root = root.resolve()

    def resolve(self, session_id: str, target: Database) -> int:
        bundle = self._bundle(session_id)
        legacy_ref = bundle["legacy_ref"]
        if isinstance(legacy_ref, str):
            match = _LEGACY_SESSION.fullmatch(legacy_ref)
            if match is not None:
                legacy_id = int(match.group(1))
                target.get_recording_session(legacy_id)
                return legacy_id
        manifest = bundle["manifest"]
        manifest_sha256 = str(bundle["manifest_sha256"])
        if _SAFE_SESSION_DIRECTORY.fullmatch(session_id) is None:
            raise ValueError("V3 session id is not safe for a compatibility directory")
        directory = self.root / f"{session_id}-{manifest_sha256[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        manifest_path = directory / "session_summary.json"
        self._publish(
            self.artifact_store.path_for(str(bundle["manifest_storage_ref"])),
            manifest_path,
            manifest_sha256,
        )
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list) or len(chunks) != len(bundle["segments"]):
            raise ValueError("V3 session manifest and segment graph disagree")
        by_index = {int(value["sequence"]): value for value in bundle["segments"]}
        for sequence, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                raise ValueError("V3 session manifest chunk is invalid")
            name = str(chunk.get("fileName") or "")
            if not name or Path(name).name != name:
                raise ValueError("V3 manifest contains an unsafe chunk name")
            segment = by_index.get(sequence)
            if segment is None:
                raise ValueError("V3 session is missing a manifest segment")
            self._publish(
                self.audio_store.path_for(str(segment["storage_key"])),
                directory / name,
                str(segment["sha256"]),
            )
        summary = ingest_session_manifest(
            target,
            manifest_path,
            device=str(manifest.get("device") or "Harmony Phone"),
            timezone_name=str(manifest.get("timezone") or "UTC"),
            ingest_method="watch_auto",
        )
        return summary.session_id

    def _bundle(self, session_id: str) -> dict[str, Any]:
        with self.database.read() as connection:
            session = connection.execute(
                "SELECT legacy_ref FROM recording_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            manifest = connection.execute(
                """
                SELECT sha256, storage_ref, entries_json FROM session_manifests
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            segments = connection.execute(
                """
                SELECT s.sequence, a.sha256, r.storage_key
                FROM capture_segments s
                JOIN audio_assets a ON a.asset_id = s.asset_id
                JOIN audio_replicas r ON r.replica_id = s.replica_id
                WHERE s.session_id = ? AND r.state = 'available'
                ORDER BY s.sequence
                """,
                (session_id,),
            ).fetchall()
        if session is None or manifest is None or not segments:
            raise ValueError("V3 session cannot be materialized because it is incomplete")
        entries = json.loads(str(manifest["entries_json"]))
        if not isinstance(entries, dict):
            raise ValueError("V3 session manifest payload is invalid")
        return {
            "legacy_ref": session["legacy_ref"],
            "manifest": entries,
            "manifest_sha256": str(manifest["sha256"]),
            "manifest_storage_ref": str(manifest["storage_ref"]),
            "segments": [dict(row) for row in segments],
        }

    @staticmethod
    def _publish(source: Path, destination: Path, expected_sha256: str) -> None:
        if destination.exists():
            if _sha256(destination) != expected_sha256:
                raise RuntimeError("existing V2 compatibility file has conflicting content")
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".part", dir=destination.parent
        )
        temporary = Path(temporary_name)
        os.close(descriptor)
        try:
            shutil.copyfile(source, temporary)
            if _sha256(temporary) != expected_sha256:
                raise RuntimeError("V2 compatibility copy failed SHA-256 verification")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["V2SessionMaterializer"]
