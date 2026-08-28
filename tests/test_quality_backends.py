from __future__ import annotations

import unittest

import numpy as np

from allday_asr.asr.quality_backends import (
    SpeechGateSettings,
    _build_speech_gate,
    _merge_speech_ranges,
    _pad_non_overlapping_ranges,
)


class QualityBackendTests(unittest.TestCase):
    def test_dual_evidence_gate_keeps_auditable_acceptance_reasons(self) -> None:
        sample_rate = 16_000
        waveform = np.full(sample_rate * 4, 0.001, dtype=np.float32)
        waveform[sample_rate : round(sample_rate * 1.3)] = 0.1
        settings = SpeechGateSettings(
            fsmn_merge_gap_ms=0,
            inference_padding_ms=100,
            speech_output_padding_ms=0,
            min_candidate_ms=800,
            min_snr_db=9.0,
            min_silero_overlap_ms=500,
        )
        candidates, speech_ranges = _build_speech_gate(
            waveform,
            sample_rate,
            [(100, 500), (1_000, 1_300), (2_000, 2_600), (3_000, 3_900)],
            [(2_050, 2_550)],
            settings=settings,
        )

        self.assertEqual(len(candidates), 4)
        self.assertFalse(candidates[0].accepted)
        self.assertIn("relative_snr", candidates[1].acceptance_reasons)
        self.assertEqual(
            candidates[2].acceptance_reasons, ("silero_confirmation",)
        )
        self.assertEqual(candidates[3].acceptance_reasons, ("duration",))
        self.assertEqual(speech_ranges, [(1_000, 1_300), (2_000, 2_600), (3_000, 3_900)])

    def test_merge_and_padding_never_duplicate_overlapping_context(self) -> None:
        merged = _merge_speech_ranges(
            [(100, 500), (900, 1_200), (1_300, 2_500)],
            3_000,
            merge_gap_ms=500,
            max_utterance_ms=1_500,
        )
        self.assertEqual(merged, [(100, 1_200), (1_300, 2_500)])
        padded = _pad_non_overlapping_ranges(merged, 3_000, padding_ms=300)
        self.assertEqual(padded, [(0, 1_250), (1_250, 2_800)])


if __name__ == "__main__":
    unittest.main()
