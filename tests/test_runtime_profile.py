from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from allday_asr.services.runtime_profile import resolve_vram_profile


class RuntimeProfileTests(unittest.TestCase):
    def test_auto_keeps_full_quality_models_and_selects_batch_profile_by_vram(self) -> None:
        for gib, expected in ((8, "compatible-8gb"), (16, "quality-16gb")):
            fake_torch = SimpleNamespace(
                cuda=SimpleNamespace(
                    is_available=lambda: True,
                    current_device=lambda: 0,
                    get_device_properties=lambda _index, gib=gib: SimpleNamespace(
                        total_memory=gib * 1024**3
                    ),
                )
            )
            with self.subTest(gib=gib), patch.dict(
                sys.modules, {"torch": fake_torch}
            ):
                self.assertEqual(resolve_vram_profile("auto"), expected)

    def test_cpu_auto_uses_compatible_profile(self) -> None:
        self.assertEqual(
            resolve_vram_profile("auto", device="cpu"), "compatible-8gb"
        )

    def test_explicit_profile_is_stable(self) -> None:
        self.assertEqual(
            resolve_vram_profile("quality-16gb", device="cpu"), "quality-16gb"
        )


if __name__ == "__main__":
    unittest.main()
