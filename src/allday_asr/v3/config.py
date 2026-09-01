from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlparse


class DeploymentMode(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class V3ConfigurationError(ValueError):
    pass


class CodexEffortSetting(StrEnum):
    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"


@dataclass(frozen=True)
class CodexReminderSettings:
    enabled: bool = True
    workdir: Path = Path.home() / ".alldayrecording_codex"
    model: str | None = None
    reasoning_effort: CodexEffortSetting = CodexEffortSetting.AUTO
    allow_auto_apply: bool = False

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> CodexReminderSettings:
        values = os.environ if environ is None else environ
        raw_workdir = values.get(
            "ALLDAY_V3_CODEX_WORKDIR",
            str(Path.home() / ".alldayrecording_codex"),
        ).strip()
        if not raw_workdir:
            raise V3ConfigurationError(
                "ALLDAY_V3_CODEX_WORKDIR must name the dedicated empty directory"
            )
        raw_effort = values.get(
            "ALLDAY_V3_CODEX_REASONING_EFFORT", "auto"
        ).strip().lower()
        try:
            effort = CodexEffortSetting(raw_effort)
        except ValueError as exc:
            raise V3ConfigurationError(
                "ALLDAY_V3_CODEX_REASONING_EFFORT must be auto, low, medium, "
                "high or xhigh"
            ) from exc
        settings = cls(
            enabled=_parse_bool(values.get("ALLDAY_V3_CODEX_ENABLED", "1")),
            workdir=Path(raw_workdir).expanduser(),
            model=_optional_model(values.get("ALLDAY_V3_CODEX_MODEL")),
            reasoning_effort=effort,
            allow_auto_apply=_parse_bool(
                values.get("ALLDAY_V3_CODEX_AUTO_APPLY", "0")
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if not str(self.workdir).strip():
            raise V3ConfigurationError("Codex workdir must not be empty")


@dataclass(frozen=True)
class V3Settings:
    enabled: bool = True
    deployment_mode: DeploymentMode = DeploymentMode.DEVELOPMENT
    passkey_rp_id: str | None = None
    passkey_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> V3Settings:
        values = os.environ if environ is None else environ
        raw_mode = values.get("ALLDAY_V3_DEPLOYMENT", "development").strip()
        try:
            mode = DeploymentMode(raw_mode)
        except ValueError as exc:
            raise V3ConfigurationError(
                "ALLDAY_V3_DEPLOYMENT must be development or production"
            ) from exc
        raw_origins = values.get("ALLDAY_V3_PASSKEY_ORIGINS", "")
        settings = cls(
            enabled=_parse_bool(values.get("ALLDAY_V3_ENABLED", "1")),
            deployment_mode=mode,
            passkey_rp_id=_optional(values.get("ALLDAY_V3_PASSKEY_RP_ID")),
            passkey_origins=tuple(
                origin.strip()
                for origin in raw_origins.split(",")
                if origin.strip()
            ),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.deployment_mode is not DeploymentMode.PRODUCTION:
            return
        if not self.passkey_rp_id or _is_placeholder_rp_id(self.passkey_rp_id):
            raise V3ConfigurationError(
                "production requires a real Passkey RP ID; local, test, "
                "example, invalid and IP hosts are forbidden"
            )
        if not self.passkey_origins:
            raise V3ConfigurationError(
                "production requires at least one trusted Passkey origin"
            )
        for origin in self.passkey_origins:
            if not _is_trusted_origin(origin):
                raise V3ConfigurationError(
                    f"production Passkey origin is invalid: {origin}"
                )


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise V3ConfigurationError("ALLDAY_V3_ENABLED must be a boolean")


def _optional(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip().lower()


def _optional_model(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return value.strip()


def _is_placeholder_rp_id(value: str) -> bool:
    candidate = value.strip().lower().rstrip(".")
    if "://" in candidate or "/" in candidate or "." not in candidate:
        return True
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        pass
    labels = candidate.split(".")
    blocked_suffixes = {"local", "localhost", "test", "example", "invalid"}
    blocked_hosts = {"example.com", "example.net", "example.org"}
    return (
        labels[-1] in blocked_suffixes
        or candidate in blocked_hosts
        or any(candidate.endswith(f".{host}") for host in blocked_hosts)
        or any(not label for label in labels)
    )


def _is_trusted_origin(value: str) -> bool:
    if value.startswith("ohos:app-id:"):
        return len(value) > len("ohos:app-id:")
    parsed = urlparse(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.path in {"", "/"}
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
        and not _is_placeholder_rp_id(parsed.hostname or "")
    )


__all__ = [
    "CodexEffortSetting",
    "CodexReminderSettings",
    "DeploymentMode",
    "V3ConfigurationError",
    "V3Settings",
]
