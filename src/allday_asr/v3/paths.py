from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ModelPaths:
    model_dir: Path
    config_path: Path

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> ModelPaths:
        values = os.environ if environ is None else environ
        root = project_root.resolve()
        return cls(
            model_dir=Path(values.get("ALLDAY_V3_MODEL_DIR", root / "models")),
            config_path=Path(
                values.get("ALLDAY_V3_MODEL_CONFIG", root / "allday-asr.toml")
            ),
        )

    def configure_model_cache(
        self,
        environ: MutableMapping[str, str] | None = None,
    ) -> dict[str, str]:
        values = os.environ if environ is None else environ
        self.model_dir.mkdir(parents=True, exist_ok=True)
        configured = {
            "MODELSCOPE_CACHE": str(self.model_dir / "modelscope"),
            "HF_HOME": str(self.model_dir / "huggingface"),
        }
        for name, value in configured.items():
            values.setdefault(name, value)
        return {name: values[name] for name in configured}


DEFAULT_MODEL_PATHS = ModelPaths.from_environment()
DEFAULT_CONFIG_PATH = DEFAULT_MODEL_PATHS.config_path


def configure_model_cache(
    paths: ModelPaths | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    return (paths or DEFAULT_MODEL_PATHS).configure_model_cache(environ)


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_MODEL_PATHS",
    "ModelPaths",
    "PROJECT_ROOT",
    "configure_model_cache",
]
