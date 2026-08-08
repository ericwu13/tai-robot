"""Build RegimeConfig from settings dict + optional evolved-params overlay."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

from src.regime.state_machine import RegimeConfig

EVOLVED_PARAMS_PATH = os.path.join("data", "regime", "regime_evolved_params.json")
EVOLVED_MAX_AGE_DAYS = 90


def _load_evolved_params(base_dir: str = "") -> dict | None:
    """Load evolved regime params if the file exists and is recent."""
    path = os.path.join(base_dir, EVOLVED_PARAMS_PATH) if base_dir else EVOLVED_PARAMS_PATH
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    evolved_at = data.get("evolved_at", "")
    if not evolved_at:
        return None
    try:
        dt = datetime.strptime(evolved_at, "%Y-%m-%d")
    except ValueError:
        return None
    if datetime.now() - dt > timedelta(days=EVOLVED_MAX_AGE_DAYS):
        return None
    return data


_SETTINGS_KEY_MAP = {
    "regime_adx_enter": "adx_enter",
    "regime_adx_exit": "adx_exit",
    "regime_adx_strong": "adx_strong",
    "regime_confirm_sessions": "confirm_sessions",
    "regime_flip_pause_sessions": "pause_sessions",
    "regime_max_flips_in_window": "max_flips",
    "regime_flip_window_sessions": "flip_window",
    "regime_classify_interval": "classify_interval",
    "regime_range_bias_action": "range_bias_action",
}


def build_regime_config(
    settings: dict,
    *,
    enabled: bool = False,
    long_strategy: str = "",
    short_strategy: str = "",
    base_dir: str = "",
) -> RegimeConfig:
    """Construct a RegimeConfig from the flat settings dict produced by
    ``_load_settings()`` in run_backtest.py, then overlay evolved params
    if a recent file exists.

    ``enabled``, ``long_strategy``, and ``short_strategy`` come from the
    deploy dialog (runtime choices), not settings.yaml.
    """
    kwargs: dict = {
        "enabled": enabled,
        "long_strategy": long_strategy,
        "short_strategy": short_strategy,
    }
    for settings_key, cfg_field in _SETTINGS_KEY_MAP.items():
        if settings_key in settings:
            kwargs[cfg_field] = settings[settings_key]

    evolved = _load_evolved_params(base_dir)
    if evolved:
        _EVOLVED_FIELDS = {"adx_enter", "adx_strong", "confirm_sessions"}
        for field in _EVOLVED_FIELDS:
            if field in evolved:
                val = evolved[field]
                if field == "confirm_sessions":
                    kwargs[field] = int(val)
                else:
                    kwargs[field] = float(val)

    return RegimeConfig(**kwargs)
