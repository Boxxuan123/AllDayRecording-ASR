from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

from allday_asr.config import ConfigError, config_from_mapping, load_config
from allday_asr.paths import DEFAULT_CONFIG_PATH


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid_and_hash_is_stable(self) -> None:
        first = load_config(DEFAULT_CONFIG_PATH)
        second = load_config(DEFAULT_CONFIG_PATH)
        self.assertEqual(first.sha256(), second.sha256())
        self.assertEqual(first.identity.threshold, 0.36)
        self.assertEqual(first.diarization.min_cluster_segments, 3)
        self.assertEqual(first.asr.speech_gate_min_candidate_ms, 800)
        self.assertEqual(first.asr.speech_gate_min_silero_overlap_ms, 500)

    def test_unknown_or_invalid_values_are_rejected(self) -> None:
        with self.assertRaises(ConfigError):
            config_from_mapping({"identity": {"threshold": 1.5}})
        with self.assertRaises(ConfigError):
            config_from_mapping({"runtime": {"devcie": "cpu"}})
        with self.assertRaises(ConfigError):
            config_from_mapping(
                {"asr": {"speech_gate_silero_threshold": 1.0}}
            )

    def test_custom_toml_is_loaded(self) -> None:
        path = Path(__file__).parent / f"config-{uuid4().hex}.toml"
        try:
            path.write_text(
                """config_version = 1
[runtime]
device = "cpu"
[identity]
threshold = 0.5
""",
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.runtime.device, "cpu")
            self.assertEqual(config.identity.threshold, 0.5)
            self.assertEqual(config.asr.language, "zh")
        finally:
            if path.exists():
                path.unlink()


if __name__ == "__main__":
    unittest.main()
