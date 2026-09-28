from __future__ import annotations

import sqlite3
import unittest

from allday_asr.v3.adapters.sqlite.desktop_recording_queries import DesktopRecordingQueryMixin


class _Timeline(DesktopRecordingQueryMixin):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class TimelinePagingTests(unittest.TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript("""
            CREATE TABLE recording_sessions (session_id TEXT, tombstoned_at TEXT);
            CREATE TABLE processing_runs (run_id TEXT, session_id TEXT, status TEXT, created_at TEXT);
            CREATE TABLE processing_jobs (run_id TEXT, request_json TEXT);
            CREATE TABLE speaker_tracks (speaker_track_id TEXT, session_id TEXT, run_id TEXT, label TEXT);
            CREATE TABLE speaker_cluster_memberships (speaker_track_id TEXT, cluster_id TEXT, state TEXT);
            CREATE TABLE person_cluster_links (cluster_id TEXT, person_id TEXT, status TEXT);
            CREATE TABLE persons (person_id TEXT, display_name TEXT);
            CREATE TABLE utterances (
              utterance_id TEXT, session_id TEXT, run_id TEXT, speaker_track_id TEXT,
              original_speaker_track_id TEXT, identity TEXT, original_identity TEXT,
              start_ms INTEGER, end_ms INTEGER, start_at TEXT, end_at TEXT,
              text TEXT, original_text TEXT, revision INTEGER, status TEXT,
              identity_evidence_json TEXT, evidence_json TEXT);
            INSERT INTO recording_sessions VALUES ('s', NULL);
            INSERT INTO processing_runs VALUES ('r', 's', 'succeeded', '2026-09-28');
            INSERT INTO speaker_tracks VALUES ('t', 's', 'r', 'SPEAKER_01');
        """)
        for key, start, end, text in (
            ("a", 0, 100, "first"), ("b", 0, 100, "second"),
            ("c", 100, 200, "needle"), ("d", 200, 300, "fourth"),
            ("e", 300, 400, "fifth"),
        ):
            connection.execute(
                "INSERT INTO utterances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, "s", "r", "t", None, "unknown", "unknown", start, end,
                 "2026-09-28T00:00:00", "2026-09-28T00:00:01", text, text,
                 1, "active", '{}', '{"sound_kind":"speech","audit":"large"}'),
            )
        self.query = _Timeline(connection)

    def tearDown(self) -> None:
        self.query.connection.close()

    def test_cursor_covers_ties_without_duplicates_and_searches_all_pages(self) -> None:
        cursor = None
        ids: list[str] = []
        while True:
            page = self.query.timeline_page("s", limit=2, cursor=cursor)
            ids.extend(row["utterance_id"] for row in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(ids, ["a", "b", "c", "d", "e"])
        found = self.query.timeline_page("s", limit=2, search="needle")
        self.assertEqual([row["utterance_id"] for row in found["items"]], ["c"])
        self.assertEqual(found["total"], 1)
        self.assertEqual(found["items"][0]["evidence"], {"sound_kind": "speech"})
        self.assertEqual(self.query.timeline_page("s", search="%_")["total"], 0)
        self.assertEqual(self.query.timeline_page("s", speaker="SPEAKER_01")["total"], 5)
        session_id = "00000000-0000-7000-8000-000000000001"
        track_id = "00000000-0000-7000-8000-000000000002"
        utterance_id = "00000000-0000-7000-8000-000000000003"
        self.query.connection.execute("INSERT INTO recording_sessions VALUES (?,NULL)", (session_id,))
        self.query.connection.execute(
            "INSERT INTO processing_runs VALUES ('valid-run', ?, 'succeeded', '2026-09-28')",
            (session_id,),
        )
        self.query.connection.execute(
            "INSERT INTO speaker_tracks VALUES (?, ?, 'valid-run', 'SPEAKER_01')",
            (track_id, session_id),
        )
        self.query.connection.execute(
            "INSERT INTO utterances VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (utterance_id, session_id, 'valid-run', track_id, None, 'unknown',
             'unknown', 0, 100, '2026-09-28T00:00:00+00:00',
             '2026-09-28T00:00:01+00:00', 'full evidence', 'full evidence',
             1, 'active', '{}', '{"audit":"large"}'),
        )
        self.assertEqual(self.query.timeline_utterance(session_id, utterance_id)["evidence"]["audit"], "large")
        self.assertEqual(self.query.timeline_page("s", limit=2, start_ms=200)["total"], 2)

    def test_invalid_cursor_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "cursor"):
            self.query.timeline_page("s", cursor="invalid")


if __name__ == "__main__":
    unittest.main()
