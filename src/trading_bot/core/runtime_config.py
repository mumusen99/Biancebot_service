from __future__ import annotations
import copy
import os
import threading
from pathlib import Path
from typing import Any
PROJECT_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_FILE = Path(os.getenv("TRADING_RUNTIME_CONFIG", PROJECT_ROOT / "config" / "runtime.yaml"))
LIMITS_FILE = Path(os.getenv("TRADING_HARD_LIMITS", PROJECT_ROOT / "config" / "hard_limits.yaml"))
_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}
_MTIME = 0.0

def _load_yaml(path: Path) -> dict:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for runtime configuration") from exc
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}

def _validate(cfg: dict, limits: dict) -> None:
    for section in ("risk", "engine"):
        for key, rule in limits.get(section, {}).items():
            if key not in cfg.get(section, {}):
                continue
            value = cfg[section][key]
            if not isinstance(value, (int, float)):
                raise ValueError(f"{section}.{key} must be numeric")
            if value < rule["min"] or value > rule["max"]:
                raise ValueError(f"{section}.{key}={value} outside [{rule['min']}, {rule['max']}]")

def get_runtime_config(force: bool = False) -> dict:
    global _CACHE, _MTIME
    with _LOCK:
        mtime = RUNTIME_FILE.stat().st_mtime
        if force or not _CACHE or mtime != _MTIME:
            cfg = _load_yaml(RUNTIME_FILE)
            limits = _load_yaml(LIMITS_FILE)
            _validate(cfg, limits)
            _CACHE = cfg
            _MTIME = mtime
        return copy.deepcopy(_CACHE)

def apply_candidate(candidate: Path) -> dict:
    cfg = _load_yaml(candidate)
    limits = _load_yaml(LIMITS_FILE)
    _validate(cfg, limits)
    current = get_runtime_config()
    if int(cfg.get("version", 0)) <= int(current.get("version", 0)):
        raise ValueError("candidate version must be greater than current version")
    tmp = RUNTIME_FILE.with_suffix(".tmp")
    tmp.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
    with tmp.open("r+") as fh:
        fh.flush(); os.fsync(fh.fileno())
    tmp.replace(RUNTIME_FILE)
    return get_runtime_config(force=True)
