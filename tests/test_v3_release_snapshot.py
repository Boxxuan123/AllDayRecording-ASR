from __future__ import annotations

import hashlib
import shutil
import sqlite3
import stat
import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.adapters.release_snapshot import (
    create_release_backups,
    verify_release_backup,
)


class V3ReleaseSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"v3g-release-{uuid4().hex}"
        self.root.mkdir()
        self.database = self.root / "v2.sqlite3"
        self.audio = (b"release-audio-a", b"release-audio-b")
        self._create_fixture()

    def tearDown(self) -> None:
        for path in self.root.rglob("*"):
            if path.is_file():
                path.chmod(stat.S_IWRITE | stat.S_IREAD)
        shutil.rmtree(self.root, ignore_errors=True)

    def test_creates_two_read_only_verified_snapshots_and_reuses_them(self) -> None:
        source_before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        destinations = (self.root / "backup-a", self.root / "backup-b")

        first = create_release_backups(self.database, destinations)
        second = create_release_backups(self.database, destinations)

        self.assertEqual(first[0].dataset_digest, first[1].dataset_digest)
        self.assertEqual(second[0].dataset_digest, first[0].dataset_digest)
        self.assertEqual(first[0].audio_count, 2)
        self.assertEqual(first[0].audio_bytes, sum(map(len, self.audio)))
        self.assertEqual(
            hashlib.sha256(self.database.read_bytes()).hexdigest(), source_before
        )
        for backup in first:
            self.assertEqual(verify_release_backup(backup.root), backup)
            self.assertFalse(backup.database_path.stat().st_mode & stat.S_IWUSR)
            self.assertFalse(backup.manifest_path.stat().st_mode & stat.S_IWUSR)
            for copied in (backup.root / "audio").iterdir():
                self.assertFalse(copied.stat().st_mode & stat.S_IWUSR)

    def test_rejects_nested_backup_destinations(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot contain each other"):
            create_release_backups(
                self.database,
                (self.root / "backup", self.root / "backup" / "nested"),
            )

    def test_verification_detects_audio_tampering(self) -> None:
        backup, _ = create_release_backups(
            self.database,
            (self.root / "backup-a", self.root / "backup-b"),
        )
        copied = next((backup.root / "audio").iterdir())
        copied.chmod(stat.S_IWRITE | stat.S_IREAD)
        copied.write_bytes(b"tampered")

        with self.assertRaisesRegex(ValueError, "audio size mismatch"):
            verify_release_backup(backup.root)

    def _create_fixture(self) -> None:
        paths = []
        for index, payload in enumerate(self.audio, start=1):
            path = self.root / f"audio-{index}.wav"
            path.write_bytes(payload)
            paths.append(path)
        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY);
                INSERT INTO schema_migrations VALUES (15);
                CREATE TABLE source_objects (
                    id INTEGER PRIMARY KEY,
                    sha256 TEXT NOT NULL
                );
                CREATE TABLE source_instances (
                    id INTEGER PRIMARY KEY,
                    instance_key TEXT NOT NULL,
                    source_object_id INTEGER NOT NULL,
                    source_path TEXT NOT NULL,
                    byte_size INTEGER NOT NULL
                );
                """
            )
            for index, (path, payload) in enumerate(
                zip(paths, self.audio, strict=True), start=1
            ):
                connection.execute(
                    "INSERT INTO source_objects VALUES (?, ?)",
                    (index, hashlib.sha256(payload).hexdigest()),
                )
                connection.execute(
                    "INSERT INTO source_instances VALUES (?, ?, ?, ?, ?)",
                    (index, f"instance-{index}", index, str(path), len(payload)),
                )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
