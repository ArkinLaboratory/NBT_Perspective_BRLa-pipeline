"""Configuration loading for the BRLa pipeline."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge overlay onto base; overlay wins on conflicts."""
    merged = dict(base)
    for key, val in overlay.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged


def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    """Load a config, resolving an optional `extends:` base first.

    An overlay config declares `extends: config.yaml` and restates only the
    keys it changes. This keeps config.yaml the single authoritative spec —
    an A/B config that overrides just `paths` cannot silently drift from the
    real run's models, thresholds, or blocklist.
    """
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    base_name = cfg.pop("extends", None)
    if base_name:
        base_path = Path(path).resolve().parent / base_name
        with open(base_path, "r", encoding="utf-8") as f:
            base = yaml.safe_load(f) or {}
        base.pop("extends", None)  # single level of inheritance only
        cfg = _deep_merge(base, cfg)

    cfg["_root"] = ROOT
    # Resolve paths relative to repo root
    for key, val in cfg.get("paths", {}).items():
        cfg["paths"][key] = ROOT / val
    return cfg


def env(cfg_section: dict, key: str) -> str:
    """Fetch an env var whose *name* is stored in the config."""
    var_name = cfg_section[key]
    value = os.environ.get(var_name, "")
    if not value:
        raise RuntimeError(
            f"Environment variable {var_name!r} is not set. "
            f"Export it or add it to your .env file."
        )
    return value
