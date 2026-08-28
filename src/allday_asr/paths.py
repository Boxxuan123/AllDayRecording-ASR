from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = Path(os.environ.get("ALLDAY_ASR_STATE_DIR", PROJECT_ROOT / "state"))
OUTPUT_DIR = Path(os.environ.get("ALLDAY_ASR_OUTPUT_DIR", PROJECT_ROOT / "outputs"))
MODEL_DIR = Path(os.environ.get("ALLDAY_ASR_MODEL_DIR", PROJECT_ROOT / "models"))
DEFAULT_DB_PATH = STATE_DIR / "allday_asr.sqlite3"


def ensure_runtime_dirs() -> None:
    for directory in (STATE_DIR, OUTPUT_DIR, MODEL_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def recording_output_dir(recording_id: int) -> Path:
    path = OUTPUT_DIR / f"recording-{recording_id:06d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def configure_model_cache() -> None:
    """Keep downloaded model weights inside the private project model directory."""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MODELSCOPE_CACHE", str(MODEL_DIR / "modelscope"))
    os.environ.setdefault("HF_HOME", str(MODEL_DIR / "huggingface"))

