"""銘柄ごとの日次指標（調整 OHLC 基準、pandas/numpy でベクトル化）。"""
from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view


def rolling_pct_rank(values: np.ndarray, window: int = 252, min_periods: int = 100) -> np.ndarray:
    """当日を含む過去 window 日内で、当日値以下だった割合(0-1)。"""
    n = len(values)
    if n == 0:
        return np.array([])
    padded = np.concatenate([np.full(window - 1, np.nan), values.astype(float)])
    win = sliding_window_view(padded, window)
    valid = ~np.isnan(win)
    cnt = valid.sum(axis=1)
    with np.errstate(invalid="ignore"):
        le = ((win <= values[:, None]) & valid).sum(axis=1)
    out = np.where((cnt >= min_periods) & ~np.isnan(values), le / np.maximum(cnt, 1), np.nan)
    return out


def rolling_days_since_min(values: np.ndarray, window: int = 60) -> np.ndarray:
    """直近 window 行の最安値からの経過行数（当日=0）。window 行に満たない行は NaN。"""
    n = len(values)
    if n < window:
        return np.full(n, np.nan)
    win = sliding_window_view(values.astype(float), window)  # (n-window+1, window)
    since = win[:, ::-1].argmin(axis=1).astype(float)
    return np.concatenate([np.full(window - 1, np.nan), since])


def compute_indicators(px: pd.DataFrame, bench_close: pd.Series | None, cfg) -> pd.DataFrame:
    """px: 調整済み OHLCV(index=日付)。bench_close: ベンチマーク調整終値。"""
    close, high, low, vol = px["close"], px["high"], px["low"], px["volume"]
    n = len(px)
    out = pd.DataFrame(index=px.index)
    out["close"], out["high"], out["low"], out["volume"] = close, high, low, vol
    out["prev_close"] = close.shift(1)
    out["ma20"] = close.rolling(20).mean()
    out["ma60"] = close.rolling(60).mean()
    out["ma200"] = close.rolling(200).mean()
    out["ma20_slope5"] = out["ma20"] / out["ma20"].shift(5) - 1
    out["ma60_slope10"] = out["ma60"] / out["ma60"].shift(10) - 1
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    out["atr20"] = tr.rolling(20).mean()
    out["atr_pct"] = out["atr20"] / close
    out["atr_pct_rank252"] = rolling_pct_rank(out["atr_pct"].to_numpy(), 252, 100)
    out["high60"] = high.rolling(60).max()
    out["low60"] = low.rolling(60).min()
    out["prev_high60"] = out["high60"].shift(1)
    span = out["high60"] - out["low60"]
    out["range_pos60"] = ((close - out["low60"]) / span).where(span > 0)
    out["high252"] = high.rolling(252, min_periods=cfg.indicators.min_history_days).max()
    out["dist_52w_high"] = 1 - close / out["high252"]
    out["vol_ratio20"] = vol / vol.shift(1).rolling(20).mean().where(lambda s: s > 0)
    out["ret5"] = close / close.shift(5) - 1
    out["ret20"] = close / close.shift(20) - 1
    out["ret60"] = close / close.shift(60) - 1
    if bench_close is not None and len(bench_close):
        b20 = (bench_close / bench_close.shift(20) - 1).reindex(px.index, method="ffill")
        b60 = (bench_close / bench_close.shift(60) - 1).reindex(px.index, method="ffill")
    else:
        b20 = b60 = pd.Series(np.nan, index=px.index)
    out["rel20"] = out["ret20"] - b20
    out["rel60"] = out["ret60"] - b60
    drawdown = 1 - close / out["high252"]
    out["max_drawdown_120"] = drawdown.rolling(120, min_periods=60).max()
    out["days_since_low60"] = rolling_days_since_min(low.to_numpy(), 60)
    out["hist_ok"] = np.arange(1, n + 1) >= cfg.indicators.min_history_days
    return out
