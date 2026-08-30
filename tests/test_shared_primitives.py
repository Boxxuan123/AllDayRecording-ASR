from __future__ import annotations

import hashlib
import unittest

import numpy as np

from allday_asr.application.use_cases.session_integrity import (
    mapping_discontinuities,
    verify_session_inputs,
)
from allday_asr.audio.embeddings import l2_normalize, normalize_speech_level
from allday_asr.domain.hashing import canonical_json, canonical_json_sha256
from allday_asr.domain.intervals import group_segments
from allday_asr.domain.text import levenshtein_operations, normalize_text
from allday_asr.domain.time import absolute_timestamp, format_offset
from allday_asr.exporters import (
    _absolute_timestamp as legacy_absolute_timestamp,
)
from allday_asr.exporters import _group_segments as legacy_group_segments
from allday_asr.services.enrollment import l2_normalize as legacy_l2_normalize
from allday_asr.services.evaluation import normalize_text as legacy_normalize_text
from allday_asr.services.session_readiness import (
    verify_session_inputs as legacy_verify_session_inputs,
)


class SharedPrimitiveTests(unittest.TestCase):
    def test_canonical_json_hash_preserves_protocol_bytes(self) -> None:
        value = {"b": "中文", "a": [1, True]}
        canonical = '{"a":[1,true],"b":"中文"}'

        self.assertEqual(canonical_json(value), canonical)
        self.assertEqual(
            canonical_json_sha256(value),
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_text_normalization_and_edit_breakdown_are_stable(self) -> None:
        self.assertIs(legacy_normalize_text, normalize_text)
        self.assertEqual(normalize_text(" Ａ，B ！"), "ab")
        self.assertEqual(
            levenshtein_operations("你好", "你们好"),
            {
                "distance": 1,
                "substitutions": 0,
                "deletions": 0,
                "insertions": 1,
            },
        )

    def test_interval_grouping_keeps_the_existing_gap_boundary(self) -> None:
        self.assertIs(legacy_group_segments, group_segments)
        segments = [
            {"id": 1, "start_ms": 0, "end_ms": 1_000},
            {"id": 2, "start_ms": 2_000, "end_ms": 3_000},
            {"id": 3, "start_ms": 4_001, "end_ms": 5_000},
        ]

        groups = group_segments(iter(segments), max_gap_ms=1_000)

        self.assertEqual([[item["id"] for item in group] for group in groups], [[1, 2], [3]])

    def test_absolute_time_and_offset_format_are_stable(self) -> None:
        self.assertIs(legacy_absolute_timestamp, absolute_timestamp)
        self.assertEqual(
            absolute_timestamp(
                "2026-08-24T12:23:54+00:00",
                60_000,
                "Asia/Singapore",
            ),
            "2026-08-24T20:24:54+08:00",
        )
        self.assertIsNone(absolute_timestamp("not-a-time", 0))
        self.assertEqual(format_offset(3_723_999), "01:02:03")

    def test_embedding_normalization_preserves_zero_rows_and_peak_limit(self) -> None:
        self.assertIs(legacy_l2_normalize, l2_normalize)
        normalized = l2_normalize(
            np.asarray([[3.0, 4.0], [0.0, 0.0]], dtype=np.float64)
        )
        np.testing.assert_allclose(normalized[0], [0.6, 0.8])
        np.testing.assert_array_equal(normalized[1], [0.0, 0.0])
        self.assertEqual(normalized.dtype, np.float32)

        speech = normalize_speech_level(
            np.asarray([0.0, 0.1, -0.5], dtype=np.float32)
        )
        self.assertLessEqual(float(np.max(np.abs(speech))), 0.950001)

    def test_session_mapping_reports_gaps_and_overlaps_without_io(self) -> None:
        self.assertIs(legacy_verify_session_inputs, verify_session_inputs)
        rows = [
            {"session_start_ms": 2_000, "session_end_ms": 2_500},
            {"session_start_ms": 0, "session_end_ms": 1_000},
            {"session_start_ms": 900, "session_end_ms": 1_500},
        ]

        gaps, overlaps = mapping_discontinuities(rows, duration_ms=3_000)

        self.assertEqual(gaps, [(1_500, 2_000), (2_500, 3_000)])
        self.assertEqual(overlaps, [(900, 1_000)])


if __name__ == "__main__":
    unittest.main()
