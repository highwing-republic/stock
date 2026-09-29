"""config/watch.yaml の読み込みと検証。閾値はすべて YAML が正本。"""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PATH = ROOT / "config" / "watch.yaml"


class ConfigError(ValueError):
    pass


class Cfg(dict):
    """dict にドット参照を足しただけの設定オブジェクト。"""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc
        if isinstance(value, dict) and not isinstance(value, Cfg):
            value = Cfg(value)
            self[key] = value
        return value


def _wrap(value: Any) -> Any:
    if isinstance(value, dict):
        return Cfg({k: _wrap(v) for k, v in value.items()})
    return value


def _merge(base: dict, over: dict) -> dict:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ConfigError(message)


def validate(cfg: dict) -> None:
    for section in ("calendar", "universe", "events", "prices", "indicators", "price_setup",
                    "episodes", "ranking", "performance", "margin", "pipeline", "export"):
        _require(isinstance(cfg.get(section), dict), f"config section missing: {section}")
    hh, mm = str(cfg["calendar"]["market_close"]).split(":")
    _require(0 <= int(hh) < 24 and 0 <= int(mm) < 60, "calendar.market_close must be HH:MM")
    _require(0 < cfg["prices"]["min_coverage"] <= 1, "prices.min_coverage must be in (0,1]")
    _require(cfg["pipeline"]["replay_days"] >= 30, "pipeline.replay_days must be >= 30")
    _require(sorted(cfg["price_setup"]["priority"]) == ["BREAKOUT", "RECOVERY", "TREND"],
             "price_setup.priority must list BREAKOUT/TREND/RECOVERY once each")
    _require(cfg["ranking"]["weekly_size"] > 0 and cfg["ranking"]["monthly_size"] > 0,
             "ranking sizes must be positive")
    _require(cfg["episodes"]["broken_days"] >= 1, "episodes.broken_days must be >= 1")
    _require(sorted(cfg["performance"]["horizons"]) == list(cfg["performance"]["horizons"]),
             "performance.horizons must be ascending")
    for h in cfg["performance"]["horizons"]:
        _require(isinstance(h, int) and h > 0, "performance.horizons must be positive ints")


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> Cfg:
    """リポジトリ既定の YAML を読み、path（あれば）と overrides を順に重ねる。"""
    with open(DEFAULT_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    extra = path or os.getenv("WATCH_CONFIG")
    if extra and Path(extra).resolve() != DEFAULT_PATH.resolve():
        with open(extra, encoding="utf-8") as fh:
            _merge(data, yaml.safe_load(fh) or {})
    if overrides:
        _merge(data, overrides)
    validate(data)
    return _wrap(data)
