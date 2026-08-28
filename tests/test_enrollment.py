from __future__ import annotations

import unittest
import wave
from pathlib import Path
from uuid import uuid4

import numpy as np

from allday_asr.services.enrollment import (
    collect_unique_audio_files,
    make_speech_chunks,
    normalize_speech_level,
    robust_embedding_filter,
)


class EnrollmentTests(unittest.TestCase):
    def test_collect_unique_audio_files_deduplicates_by_content(self) -> None:
        token = uuid4().hex
        paths = [Path(__file__).parent / f"{token}-{name}.wav" for name in ("one", "copy")]
        try:
            for path in paths:
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(16_000)
                    output.writeframes(b"\x00\x00" * 160)
            files, duplicates = collect_unique_audio_files(paths)
            self.assertEqual(len(files), 1)
            self.assertEqual(duplicates, 1)
        finally:
            for path in paths:
                if path.exists():
                    path.unlink()

    def test_make_speech_chunks_merges_short_remainder(self) -> None:
        samples = np.arange(11 * 16_000, dtype=np.float32)
        chunks = make_speech_chunks(samples, 16_000, [(0, 11_000)])
        self.assertEqual([len(chunk) for chunk in chunks], [80_000, 96_000])

    def test_normalize_speech_level_respects_peak_limit(self) -> None:
        samples = np.asarray([0.0, 0.1, -0.5], dtype=np.float32)
        normalized = normalize_speech_level(samples)
        self.assertLessEqual(float(np.max(np.abs(normalized))), 0.950001)

    def test_robust_embedding_filter_rejects_opposite_outlier(self) -> None:
        embeddings = np.asarray(
            [[1.0, 0.0], [0.99, 0.01], [0.98, -0.01], [-1.0, 0.0]],
            dtype=np.float32,
        )
        embeddings /= np.linalg.norm(embeddings, axis=1, keepdims=True)
        mask, _ = robust_embedding_filter(embeddings)
        self.assertEqual(mask.tolist(), [True, True, True, False])


if __name__ == "__main__":
    unittest.main()
