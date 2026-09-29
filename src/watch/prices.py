"""価格の更新（分割・配当検出つき）と調整 OHLC の読み込み。ネットワーク呼び出しは差し替え可能。"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Callable, Iterable

import pandas as pd

LOG = logging.getLogger("watch.prices")
YF_COLUMNS = ("Open", "High", "Low", "Close", "Adj Close", "Volume")
Downloader = Callable[[list, date, date], dict]


# --------------------------------------------------------------------------- yfinance ラッパー
def download_yf(tickers: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    """yfinance で日足を取得し ticker → DataFrame(Open..Volume, DatetimeIndex) にする。テストでは差し替える。"""
    import yfinance as yf

    data = yf.download(tickers=list(tickers), start=start.isoformat(), end=end.isoformat(), interval="1d",
                       group_by="ticker", auto_adjust=False, actions=False, progress=False, threads=True)
    out: dict[str, pd.DataFrame] = {}
    if data is None or len(data) == 0:
        return out
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        for ticker in tickers:
            if ticker in level0:
                out[ticker] = _clean(data[ticker])
    else:  # 1 銘柄で MultiIndex にならない場合
        out[tickers[0]] = _clean(data)
    return out


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    frame = frame[[c for c in YF_COLUMNS if c in frame.columns]]
    frame = frame.dropna(subset=["Close"]) if "Close" in frame.columns else frame.iloc[0:0]
    if getattr(frame.index, "tz", None) is not None:
        frame.index = frame.index.tz_localize(None)
    frame.index = pd.DatetimeIndex(frame.index).normalize()
    return frame


# --------------------------------------------------------------------------- DB 読み書き
def _num(value):
    return None if value is None or value != value else float(value)


def frame_rows(ticker: str, frame: pd.DataFrame) -> list[dict]:
    rows = []
    for ts, item in frame.iterrows():
        close = _num(item.get("Close"))
        if close is None or close <= 0:
            continue
        volume = _num(item.get("Volume"))
        rows.append({"ticker": ticker, "trade_date": ts.strftime("%Y-%m-%d"),
                     "open": _num(item.get("Open")), "high": _num(item.get("High")),
                     "low": _num(item.get("Low")), "close": close,
                     "adj_close": _num(item.get("Adj Close")) or close,
                     "volume": int(volume) if volume is not None else None})
    return rows


def upsert_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany("""INSERT INTO prices VALUES(:ticker,:trade_date,:open,:high,:low,:close,:adj_close,:volume)
      ON CONFLICT(ticker,trade_date) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,
      close=excluded.close,adj_close=excluded.adj_close,volume=excluded.volume""", rows)


def replace_ticker(conn: sqlite3.Connection, ticker: str, rows: list[dict]) -> None:
    conn.execute("DELETE FROM prices WHERE ticker=?", (ticker,))
    upsert_rows(conn, rows)


def load_raw(conn: sqlite3.Connection, tickers: Iterable[str]) -> dict[str, pd.DataFrame]:
    """DB の prices を ticker → 生 DataFrame(index=DatetimeIndex) で返す（調整前）。"""
    tickers = list(tickers)
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), 500):
        chunk = tickers[i:i + 500]
        marks = ",".join("?" * len(chunk))
        df = pd.read_sql_query(f"""SELECT ticker,trade_date,open,high,low,close,adj_close,volume FROM prices
                                   WHERE ticker IN ({marks}) ORDER BY ticker,trade_date""", conn, params=chunk)
        for ticker, sub in df.groupby("ticker", sort=False):
            sub = sub.set_index(pd.DatetimeIndex(pd.to_datetime(sub["trade_date"])))
            out[ticker] = sub.drop(columns=["ticker", "trade_date"])
    return out


def adjust(raw: pd.DataFrame) -> pd.DataFrame:
    """f = adj_close/close を OHLC 全体に掛けた調整 OHLC を返す。volume は生値。"""
    df = raw[(raw["close"] > 0) & raw["close"].notna() & raw["high"].notna() & raw["low"].notna()].copy()
    df = df[~df.index.duplicated(keep="last")].sort_index()
    f = (df["adj_close"] / df["close"]).where(df["adj_close"].notna() & (df["adj_close"] > 0), 1.0)
    open_ = df["open"].fillna(df["close"])
    return pd.DataFrame({"open": open_ * f, "high": df["high"] * f, "low": df["low"] * f,
                         "close": df["close"] * f, "volume": df["volume"].astype(float), "f": f,
                         "raw_close": df["close"]}, index=df.index)


def load_adjusted(conn: sqlite3.Connection, tickers: Iterable[str]) -> dict[str, pd.DataFrame]:
    return {t: adjust(df) for t, df in load_raw(conn, tickers).items() if len(df)}


# --------------------------------------------------------------------------- 更新
def needs_full_refetch(stored: pd.DataFrame | None, fresh: pd.DataFrame, cfg) -> str | None:
    """重複日で close または調整係数がずれていれば理由を返す（分割・配当反映の検出）。"""
    if stored is None or len(stored) == 0 or len(fresh) == 0:
        return None
    common = stored.index.intersection(fresh.index)
    if len(common) == 0:
        return None
    s, n = stored.loc[common], fresh.loc[common]
    close_gap = ((n["Close"] / s["close"]) - 1).abs()
    if (close_gap >= cfg.prices.split_mismatch_pct).any():
        return "close_mismatch"
    n_f = (n["Adj Close"] / n["Close"]).where(n["Adj Close"].notna(), 1.0)
    s_f = (s["adj_close"] / s["close"]).where(s["adj_close"].notna(), 1.0)
    if (((n_f / s_f) - 1).abs() >= cfg.prices.adj_mismatch_pct).any():
        return "adj_mismatch"
    return None


def update_prices(conn: sqlite3.Connection, tickers: Iterable[str], today: date, cfg, *,
                  downloader: Downloader | None = None) -> dict:
    """tickers の価格を DB に反映する。失敗は銘柄単位で握りつぶし、レポートに残す。"""
    dl = downloader or download_yf
    tickers = sorted(set(tickers))
    end = today + timedelta(days=1)
    full_start = today - timedelta(days=cfg.prices.history_days)
    stored = load_raw(conn, tickers)
    report: dict = {"requested": len(tickers), "rows": 0, "failed": [], "refetched": {}, "empty": []}

    full: list[str] = []
    incr: dict[str, date] = {}
    for t in tickers:
        df = stored.get(t)
        if df is None or len(df) == 0:
            full.append(t)
            continue
        first, last = df.index[0].date(), df.index[-1].date()
        if first > full_start + timedelta(days=30) or last < today - timedelta(days=cfg.prices.stale_refetch_days):
            full.append(t)  # 履歴不足（新規上場含む）または長期間未更新
        else:
            incr[t] = last - timedelta(days=cfg.prices.overlap_days)

    def fetch(group: list[str], start: date) -> dict:
        got: dict = {}
        for i in range(0, len(group), cfg.prices.chunk_size):
            chunk = group[i:i + cfg.prices.chunk_size]
            try:
                got.update(dl(chunk, start, end))
                missing = [t for t in chunk if t not in got]
            except Exception:
                LOG.exception("chunk download failed; retrying one by one")
                missing = chunk
            for t in missing:  # 個別に再試行
                try:
                    got.update(dl([t], start, end))
                except Exception as exc:
                    report["failed"].append(f"{t}: {exc}")
        return got

    def apply_full(t: str, frame: pd.DataFrame) -> None:
        rows = frame_rows(t, frame)
        if rows:
            replace_ticker(conn, t, rows)
            report["rows"] += len(rows)
        else:
            report["empty"].append(t)

    if full:
        fetched = fetch(full, full_start)
        for t, frame in fetched.items():
            apply_full(t, frame)
        report["empty"].extend(t for t in full if t not in fetched)
    if incr:
        for t, frame in fetch(list(incr), min(incr.values())).items():
            reason = needs_full_refetch(stored.get(t), frame, cfg)
            if reason:
                LOG.warning("%s: %s -> full refetch", t, reason)
                try:
                    full_frame = dl([t], full_start, end).get(t)
                except Exception as exc:
                    report["failed"].append(f"{t}: {exc}")
                    continue
                if full_frame is None or len(full_frame) == 0:
                    report["failed"].append(f"{t}: empty full refetch")
                    continue
                apply_full(t, full_frame)
                report["refetched"][t] = reason
            else:
                rows = frame_rows(t, frame)
                if rows:
                    upsert_rows(conn, rows)
                    report["rows"] += len(rows)
    conn.commit()
    failed = {x.split(":")[0] for x in report["failed"]}
    report["empty"] = sorted(set(report["empty"]) - failed)
    report["failed"] = sorted(report["failed"])
    return report
