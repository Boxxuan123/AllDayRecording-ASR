from __future__ import annotations

import unittest

import numpy as np

from allday_asr.services.verification import split_verification_windows


class VerificationTests(unittest.TestCase):
    def test_split_verification_windows_merges_short_tail(self) -> None:
        samples = np.zeros(9 * 16_000, dtype=np.float32)
        chunks = split_verification_windows(samples, 16_000)
        self.assertEqual([len(chunk) for chunk in chunks], [64_000, 80_000])

    def test_split_verification_windows_rejects_too_short_audio(self) -> None:
        samples = np.zeros(16_000, dtype=np.float32)
        self.assertEqual(split_verification_windows(samples, 16_000), [])


if __name__ == "__main__":
    unittest.main()
