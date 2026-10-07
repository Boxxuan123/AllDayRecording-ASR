from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from allday_asr.v3.adapters.models.asr_model_cache import _cached_modelscope_or_id


def test_model_snapshot_selection_is_deterministic(monkeypatch) -> None:
    model_id = "fixture/model"
    cache = Path("C:/synthetic-model-cache")
    root = cache / "modelscope" / "models" / "fixture--model" / "snapshots"
    first, second = root / "revision-a", root / "revision-b"
    present = {root, first}

    def is_dir(path: Path) -> bool:
        return path in present

    def iterdir(path: Path):
        return iter((first, second))

    monkeypatch.delenv("ALLDAY_MODEL_SNAPSHOT_IDS", raising=False)
    monkeypatch.delenv("ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK", raising=False)
    with patch.object(Path, "is_dir", is_dir), patch.object(Path, "iterdir", iterdir):
        assert _cached_modelscope_or_id(model_id, model_dir=cache) == str(first)
        present.add(second)
        with pytest.raises(ValueError, match="multiple cached snapshots"):
            _cached_modelscope_or_id(model_id, model_dir=cache)
        monkeypatch.setenv("ALLDAY_MODEL_SNAPSHOT_IDS", '{"fixture/model":"revision-a"}')
        assert _cached_modelscope_or_id(model_id, model_dir=cache) == str(first)
        monkeypatch.setenv("ALLDAY_MODEL_SNAPSHOT_IDS", '{"fixture/model":"missing"}')
        with pytest.raises(FileNotFoundError, match="pinned model snapshot"):
            _cached_modelscope_or_id(model_id, model_dir=cache)


def test_missing_model_cache_requires_explicit_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ALLDAY_MODEL_SNAPSHOT_IDS", raising=False)
    monkeypatch.delenv("ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK", raising=False)
    with patch.object(Path, "is_dir", return_value=False):
        with pytest.raises(FileNotFoundError, match="no cached snapshot"):
            _cached_modelscope_or_id("fixture/missing", model_dir=Path("C:/synthetic-model-cache"))
        monkeypatch.setenv("ALLDAY_ALLOW_REMOTE_MODEL_FALLBACK", "1")
        assert _cached_modelscope_or_id("fixture/missing", model_dir=Path("C:/synthetic-model-cache")) == "fixture/missing"
