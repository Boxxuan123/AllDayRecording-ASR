"""Guard evidence distinctions in the read-only diagnostic, not production behavior."""

import importlib.util
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import mkstemp

SCRIPT = Path(__file__).resolve().parents[1] / "tools/audit_speaker_turn_boundaries.py"
SPEC = importlib.util.spec_from_file_location("boundary_audit", SCRIPT)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class BoundaryAuditTest(unittest.TestCase):
    def test_positive_gap_is_not_union(self):
        self.assertEqual(
            audit.union([(0, 4000), (4500, 9000)]), [[0, 4000], [4500, 9000]]
        )
        self.assertEqual(audit.union([(0, 4000), (4000, 9000)]), [[0, 9000]])

    def test_serial_aba_is_not_regular_overlap(self):
        turns = [
            {"start_ms": 0, "end_ms": 4000, "speaker_label": "A"},
            {"start_ms": 4000, "end_ms": 4500, "speaker_label": "B"},
            {"start_ms": 4500, "end_ms": 9000, "speaker_label": "A"},
        ]
        self.assertEqual(audit.overlap_predictions(turns, 0, 9000), [])
        turns[1]["start_ms"] = 3500
        self.assertEqual(audit.overlap_predictions(turns, 0, 9000), [[3500, 4000]])

    def test_bridged_and_preserved_are_different_outcomes(self):
        turns = [
            {"start_ms": 0, "end_ms": 4000, "speaker_label": "A"},
            {"start_ms": 4000, "end_ms": 4500, "speaker_label": "B"},
            {"start_ms": 4500, "end_ms": 9000, "speaker_label": "A"},
        ]
        merged = [
            {
                "utterance_id": "a",
                "start_ms": 0,
                "end_ms": 9000,
                "original_speaker_track_id": "track-a",
            }
        ]
        result = audit.aba_candidates(turns, merged, {"track-a": "A"}, [], 0.5)
        self.assertEqual(result[0]["outcome"], "downstream_remerged")
        self.assertEqual(result[0]["token_loss"], "no_primary_token")
        separate = [
            {
                "utterance_id": "b",
                "start_ms": 4000,
                "end_ms": 4500,
                "original_speaker_track_id": "track-b",
            }
        ]
        result = audit.aba_candidates(turns, separate, {"track-b": "B"}, [], 0.5)
        self.assertEqual(result[0]["outcome"], "b_label_preserved")

    def test_300_500_1000_ms_probes(self):
        for probe in audit.threshold_probes():
            self.assertEqual(len(probe["b_token_absent_grouped_ranges"]), 1)
            self.assertEqual(len(probe["b_token_present_grouped_ranges"]), 3)
            self.assertFalse(probe["enrollment_positive_gap_is_union_merged"])

    def test_indexed_attribution_preserves_majority_and_zero_duration(self):
        turns = [
            {"start_ms": 0, "end_ms": 4000, "speaker_label": "A"},
            {"start_ms": 4000, "end_ms": 4500, "speaker_label": "B"},
            {"start_ms": 4500, "end_ms": 9000, "speaker_label": "A"},
        ]
        tokens = [
            {"start_ms": a, "end_ms": b, "text": "x"}
            for a, b in [
                (0, 4000),
                (3900, 4700),
                (4000, 4500),
                (4500, 4500),
                (9000, 9100),
            ]
        ]
        indexed = audit.attributed_tokens(tokens, turns, 0.5)
        self.assertEqual(
            [t["speaker"] for t in indexed],
            [audit._speaker_for(t, turns, 0.5) for t in tokens],
        )

    def test_database_is_readonly_and_unchanged(self):
        # Keep fixture inside an explicitly writable output directory on Windows.
        root = SCRIPT.parents[1] / "outputs"
        descriptor, filename = mkstemp(
            prefix="boundary-audit-test-", suffix=".sqlite3", dir=root
        )
        os.close(descriptor)
        path = Path(filename)
        try:
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE evidence(value INTEGER)")
                connection.execute("INSERT INTO evidence VALUES(1)")
            connection.close()
            before = audit.sha(path)
            with audit.open_readonly(path) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA query_only").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute("SELECT value FROM evidence").fetchone()[0], 1
                )
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("UPDATE evidence SET value=2")
            connection.close()
            self.assertEqual(before, audit.sha(path))
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
