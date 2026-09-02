from __future__ import annotations

import sqlite3
import unittest

from allday_asr.v3.adapters.legacy_v2.speaker_identity_backfill import (
    _eligible_windows,
    _identity_windows,
)


class LegacySpeakerIdentityBackfillTests(unittest.TestCase):
    def test_only_long_unconflicted_known_person_windows_are_eligible(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE manual_identity_annotations (
              id INTEGER PRIMARY KEY,
              session_id INTEGER NOT NULL,
              session_start_ms INTEGER NOT NULL,
              session_end_ms INTEGER NOT NULL,
              identity_label TEXT NOT NULL,
              status TEXT NOT NULL
            );
            INSERT INTO manual_identity_annotations VALUES
              (1, 7, 1000, 3000, 'mother', 'active'),
              (2, 7, 4000, 4500, 'mother', 'active'),
              (3, 7, 5000, 7000, 'father', 'active'),
              (4, 7, 6500, 7500, 'tv', 'active'),
              (5, 7, 8000, 10000, '我妈', 'active'),
              (6, 7, 11000, 13000, 'mother', 'retracted');
            """
        )

        source = _identity_windows(connection)
        eligible, short_count, conflict_count = _eligible_windows(source)

        self.assertEqual(len(source), 5)
        self.assertEqual(short_count, 1)
        self.assertEqual(conflict_count, 1)
        self.assertEqual(
            [(value.start_ms, value.end_ms) for value in eligible],
            [(1000, 3000), (8000, 10000)],
        )


if __name__ == "__main__":
    unittest.main()
