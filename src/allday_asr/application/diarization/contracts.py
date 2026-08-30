from __future__ import annotations

from enum import StrEnum


class DiarizationVersion(StrEnum):
    V2_D = "v2-d"
    V2_D_1 = "v2-d.1"
    V2_D_2 = "v2-d.2"
    V2_D_3 = "v2-d.3"


DEFAULT_DIARIZATION_VERSION = DiarizationVersion.V2_D


def require_diarization_version(
    value: DiarizationVersion | str,
) -> DiarizationVersion:
    try:
        return DiarizationVersion(value)
    except ValueError as exc:
        supported = ", ".join(item.value for item in DiarizationVersion)
        raise ValueError(
            f"不支持的 diarization pipeline_version：{value}；支持：{supported}"
        ) from exc
