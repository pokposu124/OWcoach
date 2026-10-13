"""Configuration loading. All tunables live in YAML files under analyzer/config."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

ANALYZER_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ANALYZER_ROOT / "config" / "analysis.yaml"
DEFAULT_PRICING = ANALYZER_ROOT / "config" / "pricing.yaml"
HEROES_DIR = ANALYZER_ROOT / "heroes"
PROMPTS_DIR = ANALYZER_ROOT / "prompts"


class ConfigError(Exception):
    pass


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    return data


def load_config(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("OWCOACH_CONFIG") or DEFAULT_CONFIG)
    return load_yaml(path)


def load_pricing(path: str | os.PathLike | None = None) -> dict[str, Any]:
    path = Path(path or os.environ.get("OWCOACH_PRICING") or DEFAULT_PRICING)
    if not path.exists():
        return {"models": {}}
    data = load_yaml(path)
    data.setdefault("models", {})
    return data


def fingerprint(*parts: Any) -> str:
    """Stable hash of JSON-serialisable values, used to decide whether a cached stage is still valid."""
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
