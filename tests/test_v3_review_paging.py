from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import patch

from allday_asr.v3.adapters.sqlite.desktop_review_queries import DesktopReviewQueryMixin


class _Reviews(DesktopReviewQueryMixin):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection


class ReviewPagingTests(unittest.TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(':memory:')
        connection.row_factory = sqlite3.Row
        connection.executescript('''
            CREATE TABLE utterances (
              utterance_id TEXT, session_id TEXT, revision INTEGER, status TEXT,
              text TEXT, evidence_json TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE generation_records (
              generation_id TEXT, layer TEXT, producer TEXT, model TEXT,
              producer_version TEXT, prompt_version TEXT, extractor_version TEXT);
            CREATE TABLE structured_change_proposals (
              proposal_id TEXT, generation_id TEXT, status TEXT, kind TEXT,
              payload_json TEXT, evidence_utterance_ids_json TEXT,
              created_at TEXT, resolution_reason TEXT);
            CREATE TABLE reminder_candidates (
              candidate_id TEXT, proposal_id TEXT, generation_id TEXT,
              status TEXT, created_at TEXT, title TEXT, operation TEXT,
              session_id TEXT, actor_person_id TEXT, expected_revision INTEGER,
              related_person_ids_json TEXT, scheduled_at TEXT, location TEXT,
              confidence REAL, needs_confirmation INTEGER);
            CREATE TABLE person_memory_entries (
              memory_id TEXT, revision INTEGER, person_id TEXT, kind TEXT,
              summary TEXT, confidence REAL, confirmation_status TEXT,
              status TEXT, event_id TEXT, created_at TEXT);
            CREATE TABLE persons (person_id TEXT, display_name TEXT);
            CREATE TABLE event_current_states (event_id TEXT, session_id TEXT);
            CREATE TABLE person_memory_evidence (
              memory_id TEXT, memory_revision INTEGER, utterance_id TEXT,
              created_at TEXT, link_id TEXT);
        ''')
        for number in range(1, 6):
            connection.execute(
                'INSERT INTO utterances VALUES (?,?,?,?,?,?,?,?)',
                (f'm{number}', 'session', 1, 'active', f'mapping {number}',
                 '{"annotation_review":{"candidates":[]}}',
                 f'2026-09-0{number}T00:00:00Z',
                 f'2026-09-{10-number:02}T00:00:00Z'),
            )
        connection.execute(
            'INSERT INTO generation_records VALUES (?,?,?,?,?,?,?)',
            ('reminder-generation', 'reminder', 'test', 'test', '1', '1', '1'),
        )
        for number, day in ((1, 2), (2, 4), (3, 6)):
            proposal_id = f'reminder-proposal-{number}'
            connection.execute(
                'INSERT INTO structured_change_proposals VALUES (?,?,?,?,?,?,?,?)',
                (proposal_id, 'reminder-generation', 'pending', 'reminder', '{}',
                 '["m1"]', f'2026-09-0{day}T00:00:00Z', None),
            )
            connection.execute(
                'INSERT INTO reminder_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (f'r{number}', proposal_id, 'reminder-generation',
                 'pending_confirmation', f'2026-09-0{day}T00:00:00Z',
                 f'reminder {number}', 'create', 'session', 'person', None,
                 '[]', None, None, 0.8, 1),
            )
        connection.execute(
            'INSERT INTO generation_records VALUES (?,?,?,?,?,?,?)',
            ('event-generation', 'event', 'test', 'test', '1', '1', '1'),
        )
        connection.execute(
            'INSERT INTO structured_change_proposals VALUES (?,?,?,?,?,?,?,?)',
            ('p1', 'event-generation', 'pending', 'event_operation',
             '{"operation":"create","event_kind":"decision",'
             '"patch":{"title":"decision","confidence":0.8}}',
             '["m1"]', '2026-09-07T00:00:00Z', None),
        )
        self.query = _Reviews(connection)

    def tearDown(self) -> None:
        self.query.connection.close()

    def test_mixed_review_pages_cover_all_items_in_stable_order(self) -> None:
        repository = 'allday_asr.v3.adapters.sqlite.desktop_repository.SqlitePeopleRepository'
        with (patch(f'{repository}.list_review_candidates', return_value=()),
              patch(f'{repository}.list_clusters', return_value=())):
            pages = [self.query.list_reviews(2, offset=offset) for offset in range(0, 10, 2)]
        self.assertEqual([len(page) for page in pages], [2, 2, 2, 2, 1])
        ids = [item['review_id'] for page in pages for item in page]
        self.assertEqual(ids, [
            'annotation_mapping:m1', 'annotation_mapping:m2', 'reminder:r1',
            'annotation_mapping:m3', 'annotation_mapping:m4', 'reminder:r2',
            'annotation_mapping:m5', 'reminder:r3', 'knowledge_proposal:p1',
        ])
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == '__main__':
    unittest.main()
