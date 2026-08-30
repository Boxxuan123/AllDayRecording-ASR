from __future__ import annotations

from typing import Any

from allday_asr.application.semantic.contracts import (
    DEFAULT_SEMANTIC_VERSION,
    SemanticVersion,
    require_semantic_version,
)
from allday_asr.application.semantic.versions import runner_for
from allday_asr.application.semantic.current import (
    EpisodeContractMockProvider,
    PreparedSemanticInput,
    ReplaySemanticProvider,
    SemanticProvider,
    SemanticV2E02Settings,
    SemanticV2E02Summary,
    export_manual_semantic_bundle,
    prepare_semantic_v2e02,
)
from allday_asr.storage.database import Database

SemanticSettings = SemanticV2E02Settings
SemanticSummary = SemanticV2E02Summary


def run(
    database: Database,
    recording_id: int | None,
    *,
    version: SemanticVersion | str = DEFAULT_SEMANTIC_VERSION,
    **kwargs: Any,
) -> Any:
    resolved = require_semantic_version(version)
    if resolved is not SemanticVersion.V2_E_0_2 and recording_id is None:
        raise ValueError(f"{resolved.value} 只支持 legacy recording_id")
    return runner_for(resolved)(database, recording_id, **kwargs)


def prepare(
    database: Database,
    recording_id: int | None,
    **kwargs: Any,
) -> PreparedSemanticInput:
    return prepare_semantic_v2e02(database, recording_id, **kwargs)


def export_manual_bundle(
    database: Database,
    recording_id: int | None,
    output_path,
    **kwargs: Any,
) -> dict[str, Any]:
    return export_manual_semantic_bundle(
        database,
        recording_id,
        output_path,
        **kwargs,
    )


__all__ = [
    "EpisodeContractMockProvider",
    "PreparedSemanticInput",
    "ReplaySemanticProvider",
    "SemanticProvider",
    "SemanticSettings",
    "SemanticSummary",
    "export_manual_bundle",
    "prepare",
    "run",
]
