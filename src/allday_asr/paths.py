from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    state_dir: Path
    output_dir: Path
    model_dir: Path
    config_path: Path
    database_path: Path

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        project_root: Path = PROJECT_ROOT,
    ) -> AppPaths:
        values = os.environ if environ is None else environ
        root = project_root.resolve()
        state_dir = Path(values.get("ALLDAY_ASR_STATE_DIR", root / "state"))
        output_dir = Path(values.get("ALLDAY_ASR_OUTPUT_DIR", root / "outputs"))
        model_dir = Path(values.get("ALLDAY_ASR_MODEL_DIR", root / "models"))
        config_path = Path(
            values.get("ALLDAY_ASR_CONFIG_PATH", root / "allday-asr.toml")
        )
        database_path = Path(
            values.get("ALLDAY_ASR_DB_PATH", state_dir / "allday_asr.sqlite3")
        )
        return cls(
            state_dir=state_dir,
            output_dir=output_dir,
            model_dir=model_dir,
            config_path=config_path,
            database_path=database_path,
        )

    @property
    def evaluation_dir(self) -> Path:
        return self.state_dir / "evaluations"

    def ensure_runtime_dirs(self) -> None:
        for directory in (self.state_dir, self.output_dir, self.model_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def recording_output_dir(self, recording_id: int) -> Path:
        path = self.output_dir / f"recording-{recording_id:06d}"
        path.mkdir(parents=True, exist_ok=True)
        return path

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


def default_app_paths() -> AppPaths:
    """Resolve current environment on demand, without import-order coupling."""
    return AppPaths.from_environment()


DEFAULT_PATHS = default_app_paths()
STATE_DIR = DEFAULT_PATHS.state_dir
OUTPUT_DIR = DEFAULT_PATHS.output_dir
MODEL_DIR = DEFAULT_PATHS.model_dir
DEFAULT_DB_PATH = DEFAULT_PATHS.database_path
DEFAULT_CONFIG_PATH = DEFAULT_PATHS.config_path
EVALUATION_DIR = DEFAULT_PATHS.evaluation_dir


def ensure_runtime_dirs(paths: AppPaths | None = None) -> None:
    (paths or DEFAULT_PATHS).ensure_runtime_dirs()


def recording_output_dir(
    recording_id: int,
    *,
    paths: AppPaths | None = None,
) -> Path:
    return (paths or DEFAULT_PATHS).recording_output_dir(recording_id)


def configure_model_cache(
    paths: AppPaths | None = None,
    *,
    environ: MutableMapping[str, str] | None = None,
) -> dict[str, str]:
    """Explicitly configure private model caches immediately before model loading."""
    return (paths or DEFAULT_PATHS).configure_model_cache(environ)


__all__ = [
    "AppPaths",
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_DB_PATH",
    "DEFAULT_PATHS",
    "EVALUATION_DIR",
    "MODEL_DIR",
    "OUTPUT_DIR",
    "PROJECT_ROOT",
    "STATE_DIR",
    "configure_model_cache",
    "default_app_paths",
    "ensure_runtime_dirs",
    "recording_output_dir",
]
