from __future__ import annotations

import unittest
import json
from pathlib import Path
from uuid import uuid4

import numpy as np

from allday_asr.audio.tools import sha256_file
from allday_asr.services.verification import (
    load_active_self_threshold,
    split_verification_windows,
)


class VerificationTests(unittest.TestCase):
    def test_active_threshold_must_match_the_current_voiceprint(self) -> None:
        suffix = uuid4().hex
        voiceprint = Path(__file__).parent / f"voiceprint-{suffix}.npz"
        other = Path(__file__).parent / f"voiceprint-other-{suffix}.npz"
        policy = Path(__file__).parent / f"identity-policy-{suffix}.json"
        try:
            voiceprint.write_bytes(b"current voiceprint")
            other.write_bytes(b"other voiceprint")
            policy.write_text(
                json.dumps(
                    {
                        "accepted": True,
                        "blockers": [],
                        "self_threshold": 0.42,
                        "voiceprint_sha256": sha256_file(voiceprint),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                load_active_self_threshold(voiceprint, policy_path=policy),
                0.42,
            )
            self.assertIsNone(
                load_active_self_threshold(other, policy_path=policy)
            )
        finally:
            for path in (voiceprint, other, policy):
                if path.exists():
                    path.unlink()

    def test_split_verification_windows_merges_short_tail(self) -> None:
        samples = np.zeros(9 * 16_000, dtype=np.float32)
        chunks = split_verification_windows(samples, 16_000)
        self.assertEqual([len(chunk) for chunk in chunks], [64_000, 80_000])

    def test_split_verification_windows_rejects_too_short_audio(self) -> None:
        samples = np.zeros(16_000, dtype=np.float32)
        self.assertEqual(split_verification_windows(samples, 16_000), [])


if __name__ == "__main__":
    unittest.main()
