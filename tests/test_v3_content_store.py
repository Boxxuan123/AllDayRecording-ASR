from __future__ import annotations

from tests.temp_workspace import TEST_WORKSPACE_ROOT, cleanup_test_case, test_directory

import hashlib
import unittest

from allday_asr.v3.adapters.files import ContentAddressedStore


TEST_ROOT = TEST_WORKSPACE_ROOT


class V3ContentStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = test_directory(self, TEST_ROOT, "v3b-store")
        self.store = ContentAddressedStore(self.directory / "content")

    def tearDown(self) -> None:
        cleanup_test_case(self)

    def test_constructor_is_empty_and_repeated_put_is_content_addressed(self) -> None:
        self.assertFalse(self.directory.exists())
        payload = b"immutable-v3-audio"

        first = self.store.put_bytes(payload)
        second = self.store.put_bytes(payload)

        self.assertEqual(first, second)
        self.assertEqual(first.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(self.store.path_for(first.storage_key).read_bytes(), payload)

    def test_put_file_rejects_digest_mismatch(self) -> None:
        self.directory.mkdir()
        source = self.directory / "source.m4a"
        source.write_bytes(b"audio")

        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.store.put_file(source, expected_sha256="0" * 64)

        self.assertFalse((self.directory / "content").exists())

    def test_existing_entry_corruption_is_detected(self) -> None:
        stored = self.store.put_bytes(b"original")
        self.store.path_for(stored.storage_key).write_bytes(b"tampered")

        with self.assertRaisesRegex(RuntimeError, "corrupted"):
            self.store.put_bytes(b"original")

    def test_storage_key_cannot_escape_root(self) -> None:
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.store.path_for("../outside")


if __name__ == "__main__":
    unittest.main()
