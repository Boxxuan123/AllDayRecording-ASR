from __future__ import annotations

from enum import StrEnum


class SemanticVersion(StrEnum):
    V2_E_0 = "v2-e.0"
    V2_E_0_1 = "v2-e.0.1"
    V2_E_0_2 = "v2-e.0.2"


DEFAULT_SEMANTIC_VERSION = SemanticVersion.V2_E_0_2


def require_semantic_version(
    value: SemanticVersion | str,
) -> SemanticVersion:
    try:
        return SemanticVersion(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in SemanticVersion)
        raise ValueError(
            f"不支持的 semantic pipeline_version：{value}；支持：{supported}"
        ) from exc
