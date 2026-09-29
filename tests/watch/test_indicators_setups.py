from datetime import date

import numpy as np
import pandas as pd
from synth import business_days, make_path, price_rows

from src.watch import prices
from src.watch.indicators import compute_indicators, rolling_days_since_min, rolling_pct_rank
from src.watch.setups import active_setups, evaluate_setups, setup_changes


def adj_frame(closes, vol=1000.0, start=date(2025, 1, 6)):
    days = business_days(start, len(closes))
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
                         "volume": pd.Series(vol, index=idx, dtype=float)})


def test_ma_atr_and_relative_return(cfg):
    closes = np.arange(1, 301, dtype=float) + 100
    px = adj_frame(closes)
    bench = pd.Series(np.full(300, 200.0), index=px.index)
    ind = compute_indicators(px, bench, cfg)
    assert ind["ma20"].iloc[-1] == closes[-20:].mean()
    assert ind["ma200"].iloc[-1] == closes[-200:].mean()
    assert np.isnan(ind["ma200"].iloc[198]) and not np.isnan(ind["ma200"].iloc[199])
    assert abs(ind["ma20_slope5"].iloc[-1] - (closes[-20:].mean() / closes[-25:-5].mean() - 1)) < 1e-12
    tr = [max(closes[i] * 0.02, abs(closes[i] * 1.01 - closes[i - 1]), abs(closes[i] * 0.99 - closes[i - 1]))
          for i in range(300 - 20, 300)]
    assert abs(ind["atr20"].iloc[-1] - np.mean(tr)) < 1e-9
    assert abs(ind["atr_pct"].iloc[-1] - ind["atr20"].iloc[-1] / closes[-1]) < 1e-12
    assert abs(ind["rel20"].iloc[-1] - (closes[-1] / closes[-21] - 1)) < 1e-12   # ベンチ横ばい
    assert abs(ind["ret5"].iloc[-1] - (closes[-1] / closes[-6] - 1)) < 1e-12
    assert abs(ind["high60"].iloc[-1] - (closes[-60:] * 1.01).max()) < 1e-9
    assert abs(ind["dist_52w_high"].iloc[-1] - (1 - closes[-1] / (closes[-252:] * 1.01).max())) < 1e-12
    assert not ind["hist_ok"].iloc[198] and ind["hist_ok"].iloc[199]
    assert ind["days_since_low60"].iloc[-1] == 59


def test_volume_ratio_excludes_today(cfg):
    px = adj_frame(np.full(60, 100.0))
    px.iloc[-1, px.columns.get_loc("volume")] = 3000.0
    assert compute_indicators(px, None, cfg)["vol_ratio20"].iloc[-1] == 3.0


def test_range_pos_none_when_flat(cfg):
    px = adj_frame(np.full(80, 100.0)).assign(high=100.0, low=100.0)
    assert np.isnan(compute_indicators(px, None, cfg)["range_pos60"].iloc[-1])


def test_rolling_helpers():
    x = np.array([5.0, 3.0, 4.0, 1.0, 2.0])
    r = rolling_pct_rank(x, window=3, min_periods=1)
    assert r[3] == 1 / 3 and r[2] == 2 / 3 and r[0] == 1.0
    d = rolling_days_since_min(np.array([3.0, 1.0, 2.0, 5.0]), window=3)
    assert np.isnan(d[0]) and np.isnan(d[1]) and d[2] == 1 and d[3] == 2


def test_split_does_not_create_false_drop(cfg):
    days = business_days(date(2025, 1, 6), 260)
    adj = make_path(260, 8, 1000, 0.001, 0.004)
    rows = price_rows("1001.T", days, adj, split_at=200)
    raw = pd.DataFrame(rows).set_index(pd.DatetimeIndex(pd.to_datetime([r["trade_date"] for r in rows])))
    ind = compute_indicators(prices.adjust(raw), None, cfg)
    assert ind["ret5"].abs().max() < 0.08 and ind["max_drawdown_120"].max() < 0.2
    unadjusted = compute_indicators(prices.adjust(raw.assign(adj_close=raw["close"])), None, cfg)
    assert unadjusted["ret5"].min() < -0.4       # 調整しなければ -50% が出る（＝調整が効いている）


# ------------------------------------------------------------------ Setup
def neutral(n=260):
    idx = pd.DatetimeIndex(pd.to_datetime(business_days(date(2025, 1, 6), n)))
    return pd.DataFrame(index=idx, data={
        "close": 100.0, "high": 101.0, "low": 99.0, "volume": 1000.0, "prev_close": 100.0, "ma20": 100.0,
        "ma60": 100.0, "ma200": 100.0, "ma20_slope5": 0.0, "ma60_slope10": 0.0, "atr_pct_rank252": 0.9,
        "range_pos60": 0.3, "dist_52w_high": 0.3, "ret5": 0.0, "rel20": -0.01, "rel60": -0.01,
        "max_drawdown_120": 0.05, "days_since_low60": 3.0, "prev_high60": 105.0, "high60": 105.0, "hist_ok": True})


def setrow(df, sl, **vals):
    for col, val in vals.items():
        df.iloc[sl, df.columns.get_loc(col)] = val


def test_breakout_recovery_trend_and_priority(cfg):
    df = neutral()
    setrow(df, -1, close=102.0, ma20=100.0, ma20_slope5=0.01, range_pos60=0.8, atr_pct_rank252=0.2)
    out = evaluate_setups(df, cfg)
    assert out["breakout"].iloc[-1] and out["primary"].iloc[-1] == "BREAKOUT" and not out["trend"].iloc[-1]
    df2 = df.copy()
    setrow(df2, -1, ret5=0.16)                      # 5 日で 15% 以上の急騰は除外
    assert not evaluate_setups(df2, cfg)["breakout"].iloc[-1]
    df3 = neutral()
    setrow(df3, -1, close=101.0, ma20_slope5=0.01, max_drawdown_120=0.2, days_since_low60=12.0, rel20=0.02)
    o3 = evaluate_setups(df3, cfg)
    assert o3["recovery"].iloc[-1] and o3["primary"].iloc[-1] == "RECOVERY"
    df4 = neutral()
    setrow(df4, -1, close=110.0, ma20=105.0, ma60=100.0, ma60_slope10=0.02, rel60=0.05, dist_52w_high=0.03,
           ma20_slope5=0.01, range_pos60=0.95, atr_pct_rank252=0.1)
    o4 = evaluate_setups(df4, cfg)
    assert o4["trend"].iloc[-1] and o4["breakout"].iloc[-1] and o4["primary"].iloc[-1] == "BREAKOUT"
    assert active_setups(o4.iloc[-1]) == ["BREAKOUT", "TREND"]


def test_insufficient_history_blocks_setups(cfg):
    df = neutral()
    df["hist_ok"] = False
    setrow(df, -1, close=110.0, ma20=105.0, ma60=100.0, ma60_slope10=0.02, rel60=0.05, dist_52w_high=0.03)
    out = evaluate_setups(df, cfg)
    assert out["primary"].iloc[-1] is None and out["insufficient"].iloc[-1]


def test_milestones_improvement_and_history(cfg):
    df = neutral(30)
    setrow(df, slice(20, 25), close=101.0, ma20_slope5=0.01, max_drawdown_120=0.2, days_since_low60=12.0, rel20=0.02)
    setrow(df, slice(25, None), close=110.0, ma20=105.0, ma60=100.0, ma60_slope10=0.02, rel60=0.05,
           dist_52w_high=0.03, ma20_slope5=0.01, range_pos60=0.5)
    out = evaluate_setups(df, cfg)
    assert out["improved"].iloc[20]                        # なし → RECOVERY
    assert out["primary"].iloc[24] == "RECOVERY" and out["primary"].iloc[25] == "TREND"
    assert out["improved"].iloc[25]                        # RECOVERY → TREND
    assert out["new_high60"].iloc[25] and out["improve_recent"].iloc[29]
    ch = setup_changes("1001", out)
    d20, d25 = out.index[20].strftime("%Y-%m-%d"), out.index[25].strftime("%Y-%m-%d")
    kinds = [(c["date"], c["kind"]) for c in ch]
    assert (d20, "SETUP") in kinds and (d25, "SETUP") in kinds
    assert any(c["kind"] == "MILESTONE" and c["milestone"] == "NEW_HIGH60" and c["date"] == d25 for c in ch)
