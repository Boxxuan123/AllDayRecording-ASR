from __future__ import annotations

import unittest

import numpy as np

from allday_asr.services.voice_library import (
    _automatic_session_split,
    _select_representative_embeddings,
)


class VoiceLibraryTests(unittest.TestCase):
    def test_auto_split_is_session_level_and_deterministic(self) -> None:
        self.assertEqual(_automatic_session_split(5), "holdout")
        self.assertEqual(_automatic_session_split(6), "accepted")

    def test_representative_embedding_limit(self) -> None:
        rng = np.random.default_rng(7)
        values = rng.normal(size=(20, 8)).astype(np.float32)
        selected = _select_representative_embeddings(values, 6)
        self.assertEqual(selected.shape, (6, 8))
        np.testing.assert_allclose(np.linalg.norm(selected, axis=1), 1.0, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
