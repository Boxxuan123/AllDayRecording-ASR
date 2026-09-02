from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from allday_asr.v3.paths import PROJECT_ROOT
from allday_asr.v3 import CONTRACT_VERSION, PROJECTION_VERSION
from allday_asr.v3.config import V3ConfigurationError, V3Settings


@dataclass(frozen=True)
class V3Runtime:
    contract_version: str
    projection_version: int
    deployment_mode: str
    state: str = "empty_ready"

    def as_dict(self) -> dict[str, str | int | bool]:
        return {
            "contract_version": self.contract_version,
            "projection_version": self.projection_version,
            "deployment_mode": self.deployment_mode,
            "enabled": True,
            "state": self.state,
        }


def start_empty_runtime(
    settings: V3Settings,
    *,
    contract_root: Path | None = None,
) -> V3Runtime:
    """Start the Phase A composition root without opening DBs or listeners."""
    if not settings.enabled:
        raise V3ConfigurationError(
            "V3 is disabled; set ALLDAY_V3_ENABLED=1 for the parallel skeleton"
        )
    root = contract_root or PROJECT_ROOT / "contracts" / "v3"
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract_version") != CONTRACT_VERSION:
        raise V3ConfigurationError("V3 manifest contract version mismatch")
    if manifest.get("projection_version") != PROJECTION_VERSION:
        raise V3ConfigurationError("V3 manifest projection version mismatch")
    return V3Runtime(
        contract_version=CONTRACT_VERSION,
        projection_version=PROJECTION_VERSION,
        deployment_mode=settings.deployment_mode.value,
    )


__all__ = ["V3Runtime", "start_empty_runtime"]
