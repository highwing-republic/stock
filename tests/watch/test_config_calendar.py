from datetime import date

import pytest
import yaml

from src.watch.calendar import MarketCalendar
from src.watch.config import ConfigError, DEFAULT_PATH, load_config


def test_yaml_defaults_match_spec(cfg):
    assert cfg.events.consecutive_window_days == 30
    assert cfg.events.large_increase_pt == 1.0
    assert cfg.events.trigger_types == ["NEW_HOLDER", "LARGE_INCREASE", "CONSECUTIVE_INCREASE"]
    assert cfg.prices.history_days == 560 and cfg.prices.split_mismatch_pct == 0.02 and cfg.prices.min_coverage == 0.8
    assert cfg.price_setup.priority == ["BREAKOUT", "TREND", "RECOVERY"]
    assert cfg.episodes.candidate_expiry_days == 20 and cfg.episodes.max_watch_days == 60
    assert cfg.episodes.overextended_return == 0.20 and cfg.episodes.broken_days == 3
    assert cfg.ranking.weekly_size == 5 and cfg.ranking.monthly_size == 10
    assert cfg.ranking.weekly_hysteresis == 8 and cfg.pipeline.replay_days == 180
    assert cfg.margin.enabled is True and cfg.universe.min_members == 350


def test_overrides_and_validation(tmp_path):
    cfg = load_config(overrides={"episodes": {"broken_days": 5}})
    assert cfg.episodes.broken_days == 5 and cfg.episodes.max_watch_days == 60
    with pytest.raises(ConfigError):
        load_config(overrides={"prices": {"min_coverage": 2}})
    with pytest.raises(ConfigError):
        load_config(overrides={"price_setup": {"priority": ["BREAKOUT"]}})
    extra = tmp_path / "x.yaml"
    extra.write_text(yaml.safe_dump({"ranking": {"weekly_size": 3}}), encoding="utf-8")
    assert load_config(extra).ranking.weekly_size == 3
    assert DEFAULT_PATH.exists()


def test_event_market_date_boundaries():
    cal = MarketCalendar([], ["2026-11-03"], "15:30")
    assert cal.event_market_date("2026-09-29 15:29") == date(2026, 9, 29)      # 火曜 引け前 → 当日
    assert cal.event_market_date("2026-09-29 15:30") == date(2026, 9, 30)      # 引け以降 → 翌営業日
    assert cal.event_market_date("2026-09-29T09:00:00") == date(2026, 9, 29)
    assert cal.event_market_date("2026-09-26 10:00") == date(2026, 9, 28)      # 土曜 → 月曜
    assert cal.event_market_date("2026-09-25 16:00") == date(2026, 9, 28)      # 金曜引け後 → 月曜
    assert cal.event_market_date("2026-11-02 16:00") == date(2026, 11, 4)      # 祝日(11/3)を飛ばす
    assert cal.event_market_date("2026-11-03 10:00") == date(2026, 11, 4)      # 祝日当日 → 翌営業日
    assert cal.event_market_date("bad") is None


def test_benchmark_dates_define_business_days():
    bench = [date(2026, 9, 24), date(2026, 9, 25), date(2026, 9, 29)]  # 9/28 は市場休場扱い
    cal = MarketCalendar(bench, [], "15:30")
    assert not cal.is_business_day(date(2026, 9, 28))
    assert cal.event_market_date("2026-09-25 16:00") == date(2026, 9, 29)
    assert cal.prev_business_day(date(2026, 9, 29)) == date(2026, 9, 25)
    assert cal.is_business_day(date(2026, 9, 30))   # 価格より先は平日補完
    assert not cal.is_business_day(date(2026, 10, 3))
    assert cal.business_days(date(2026, 9, 25), date(2026, 9, 29)) == [date(2026, 9, 25), date(2026, 9, 29)]
