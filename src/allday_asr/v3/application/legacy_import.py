from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class LegacyImportCommand:
    source_database: Path
    source_namespace: str | None = None


@dataclass(frozen=True)
class LegacyImportResult:
    import_id: str
    source_database_sha256: str
    source_schema_version: int
    created: dict[str, int]
    existing: dict[str, int]
    issues: tuple[dict[str, Any], ...]
    unmapped: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "source_database_sha256": self.source_database_sha256,
            "source_schema_version": self.source_schema_version,
            "created": dict(self.created),
            "existing": dict(self.existing),
            "issues": list(self.issues),
            "unmapped": dict(self.unmapped),
        }


class LegacyImporterPort(Protocol):
    def import_database(self, command: LegacyImportCommand) -> LegacyImportResult: ...


class ImportLegacyV2:
    def __init__(self, importer: LegacyImporterPort) -> None:
        self._importer = importer

    def execute(self, command: LegacyImportCommand) -> LegacyImportResult:
        if not command.source_database.is_file():
            raise FileNotFoundError(command.source_database)
        return self._importer.import_database(command)


__all__ = ["ImportLegacyV2", "LegacyImportCommand", "LegacyImportResult"]
