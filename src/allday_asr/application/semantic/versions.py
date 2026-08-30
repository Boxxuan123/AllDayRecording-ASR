from __future__ import annotations

from collections.abc import Callable
from typing import Any

from allday_asr.application.semantic.contracts import (
    SemanticVersion,
    require_semantic_version,
)

SemanticCallable = Callable[..., Any]


def runner_for(version: SemanticVersion | str) -> SemanticCallable:
    resolved = require_semantic_version(version)
    if resolved is SemanticVersion.V2_E_0:
        from allday_asr.application.semantic.legacy.v2e0 import run_semantic_v2e0

        return run_semantic_v2e0
    if resolved is SemanticVersion.V2_E_0_1:
        from allday_asr.application.semantic.legacy.v2e01 import run_semantic_v2e01

        return run_semantic_v2e01
    if resolved is SemanticVersion.V2_E_0_2:
        from allday_asr.application.semantic.current import run_semantic_v2e02

        return run_semantic_v2e02
    raise AssertionError(f"未处理 semantic 版本：{resolved}")


def overview_for(version: SemanticVersion | str) -> SemanticCallable:
    resolved = require_semantic_version(version)
    if resolved is SemanticVersion.V2_E_0:
        from allday_asr.application.semantic.legacy.v2e0 import semantic_overview

        return semantic_overview
    if resolved is SemanticVersion.V2_E_0_1:
        from allday_asr.application.semantic.legacy.v2e01 import semantic_overview

        return semantic_overview
    if resolved is SemanticVersion.V2_E_0_2:
        from allday_asr.application.semantic.current import semantic_overview

        return semantic_overview
    raise AssertionError(f"未处理 semantic 版本：{resolved}")
