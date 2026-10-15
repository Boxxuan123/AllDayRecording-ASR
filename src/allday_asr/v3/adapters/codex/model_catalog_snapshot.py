"""Reuse a fresh model catalog without altering the shared CLI-version cache."""

import hashlib
import json
import os
import re
import tempfile
import time
import tomllib
from datetime import datetime
from pathlib import Path


def snapshot_catalog(directory, model, *, home=None, now=None):
    home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    now = time.time() if now is None else now
    try:
        config_path = home / "config.toml"
        config = (
            tomllib.loads(config_path.read_text("utf-8"))
            if config_path.exists()
            else {}
        )
        # Respect custom catalogs/providers/profiles; their resolution belongs to the SDK.
        if any(
            config.get(k)
            for k in ("model_catalog_json", "model_catalog_url", "profile")
        ):
            return None
        if config.get("model_provider", "openai") != "openai":
            return None
        cache = json.loads((home / "models_cache.json").read_text("utf-8"))
        age = (
            now
            - datetime.fromisoformat(
                cache["fetched_at"].replace("Z", "+00:00")
            ).timestamp()
        )
        if not 0 <= age <= 3600:
            return None
        models = cache["models"]
        if not isinstance(models, list) or not any(
            m.get("slug") == model for m in models
        ):
            return None
        # Preserve catalog metadata verbatim; never spoof a runtime/cache version.
        data = json.dumps(
            {"models": models}, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    digest = hashlib.sha256(data).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + ".catalog.json")
    if not target.exists():
        with tempfile.NamedTemporaryFile(
            dir=directory, suffix=".part", delete=False
        ) as stream:
            stream.write(data)
            part = Path(stream.name)
        part.replace(target)
    return {"path": str(target.resolve()), "sha256": digest}


def configured_mcp_overrides(*, home=None):
    """Disable user-configured MCP servers only inside the bounded investigator."""
    home = Path(home or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        config = tomllib.loads((home / "config.toml").read_text("utf-8"))
        servers = config.get("mcp_servers", {})
        if not isinstance(servers, dict):
            return []
        return [
            "mcp_servers." + name + ".enabled=false"
            for name in sorted(servers)
            if re.fullmatch(r"[A-Za-z0-9_-]+", name)
        ]
    except (OSError, ValueError, TypeError):
        return []
