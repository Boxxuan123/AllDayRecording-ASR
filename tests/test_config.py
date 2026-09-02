from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.v3.model_config import (
    ModelConfigError,
    load_model_config,
    model_config_from_mapping,
)
from allday_asr.v3.paths import DEFAULT_CONFIG_PATH


class ModelConfigTests(unittest.TestCase):
    def test_default_model_config_is_valid(self) -> None:
        config = load_model_config(DEFAULT_CONFIG_PATH)
        self.assertEqual(config.asr.speech_gate_min_candidate_ms, 800)
        self.assertEqual(config.asr.speech_gate_min_silero_overlap_ms, 500)
        self.assertEqual(config.diarization.backend, "pyannote-community-1")

    def test_unknown_or_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(ModelConfigError):
            model_config_from_mapping({"runtime": {"devcie": "cpu"}})
        with self.assertRaises(ModelConfigError):
            model_config_from_mapping(
                {"asr": {"speech_gate_silero_threshold": 1.0}}
            )
        with self.assertRaises(ModelConfigError):
            model_config_from_mapping({"diarization": {"num_speakers": 0}})

    def test_custom_toml_is_loaded(self) -> None:
        path = Path(__file__).parent / f"config-{uuid4().hex}.toml"
        try:
            path.write_text(
                """config_version = 1
[runtime]
device = "cpu"
[diarization]
min_speakers = 2
max_speakers = 4
""",
                encoding="utf-8",
            )
            config = load_model_config(path)
            self.assertEqual(config.runtime.device, "cpu")
            self.assertEqual(config.diarization.min_speakers, 2)
            self.assertEqual(config.asr.language, "zh")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
