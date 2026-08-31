from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from allday_asr.paths import PROJECT_ROOT
from allday_asr.v3.adapters.files import ContentAddressedStore
from allday_asr.v3.adapters.legacy_v2 import LegacyV2Importer
from allday_asr.v3.adapters.sqlite import SqliteUnitOfWork, V3Database
from allday_asr.v3.application import ImportLegacyV2


@dataclass(frozen=True)
class V3CorePaths:
    state_dir: Path
    database_path: Path
    audio_store_path: Path
    artifact_store_path: Path

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> V3CorePaths:
        root = state_dir.resolve()
        return cls(
            state_dir=root,
            database_path=root / "core.sqlite3",
            audio_store_path=root / "audio",
            artifact_store_path=root / "artifacts",
        )

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> V3CorePaths:
        values = os.environ if environ is None else environ
        state_dir = Path(
            values.get("ALLDAY_V3_STATE_DIR", project_root / "state" / "v3")
        )
        return cls.from_state_dir(state_dir)


@dataclass(frozen=True)
class V3Core:
    paths: V3CorePaths
    database: V3Database
    audio_store: ContentAddressedStore
    artifact_store: ContentAddressedStore
    import_legacy_v2: ImportLegacyV2

    def initialize(self) -> int:
        """Create only V3-owned state and migrate it to the latest schema."""
        version = self.database.initialize()
        self.audio_store.initialize()
        self.artifact_store.initialize()
        return version


def compose_v3_core(paths: V3CorePaths | None = None) -> V3Core:
    """Wire the V3 Core without opening databases or creating directories."""
    selected = paths or V3CorePaths.from_environment()
    database = V3Database(selected.database_path)
    audio_store = ContentAddressedStore(selected.audio_store_path)
    artifact_store = ContentAddressedStore(selected.artifact_store_path)
    importer = LegacyV2Importer(
        lambda: SqliteUnitOfWork(database),
        audio_store,
        artifact_store,
    )
    return V3Core(
        paths=selected,
        database=database,
        audio_store=audio_store,
        artifact_store=artifact_store,
        import_legacy_v2=ImportLegacyV2(importer),
    )


__all__ = ["V3Core", "V3CorePaths", "compose_v3_core"]
