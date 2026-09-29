from datetime import date

import numpy as np
import pandas as pd
from synth import business_days, insert_prices, make_path, price_rows

from src.radar import connect
from src.watch import prices
from src.watch.config import load_config


def yf_frame(rows):
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))
    df = df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close",
                            "adj_close": "Adj Close", "volume": "Volume"})
    return df[["Open", "High", "Low", "Close", "Adj Close", "Volume"]]


class FakeDL:
    """サーバ側の真値を返す偽ダウンローダ（ネットワークには触れない）。"""

    def __init__(self, server, fail=()):
        self.server, self.fail, self.calls = server, set(fail), []

    def __call__(self, tickers, start, end):
        self.calls.append((tuple(tickers), start))
        if len(tickers) > 1 and self.fail & set(tickers):
            raise RuntimeError("chunk boom")
        out = {}
        for t in tickers:
            if t in self.fail:
                raise RuntimeError(f"{t} boom")
            rows = [r for r in self.server.get(t, []) if start.isoformat() <= r["trade_date"] < end.isoformat()]
            if rows:
                out[t] = yf_frame(rows)
        return out


def test_adjust_uses_adj_close_ratio():
    days = business_days(date(2026, 1, 5), 10)
    rows = price_rows("X.T", days, np.full(10, 100.0), split_at=5)
    raw = pd.DataFrame(rows).set_index(pd.DatetimeIndex(pd.to_datetime([r["trade_date"] for r in rows])))
    adj = prices.adjust(raw)
    assert np.allclose(adj["close"], 100.0)                 # 分割前後で連続
    assert np.allclose(adj["high"] / adj["close"], raw["high"] / raw["close"])
    assert (adj["volume"] == raw["volume"]).all()


def test_incremental_update_and_split_full_refetch(tmp_path):
    cfg = load_config(overrides={"prices": {"history_days": 120}})
    conn = connect(tmp_path / "p.db")
    days = business_days(date(2026, 1, 5), 150)
    adj = make_path(150, 1, 1000)
    # DB は分割前の生値(1 倍)で保存済み。サーバ側は idx118 で 2:1 分割（過去の生値が 2 倍表示）
    stored = price_rows("1001.T", days[:120], adj[:120])
    stored += price_rows("1002.T", days[:120], make_path(150, 2, 500)[:120])
    insert_prices(conn, stored)
    server = {"1001.T": price_rows("1001.T", days, adj, split_at=118),
              "1002.T": price_rows("1002.T", days, make_path(150, 2, 500))}
    today = days[125]
    rep = prices.update_prices(conn, ["1001.T", "1002.T", "1306.T"], today, cfg, downloader=FakeDL(server))
    assert rep["refetched"] == {"1001.T": "close_mismatch"}
    assert "1306.T" in rep["empty"] and rep["failed"] == []
    got = prices.load_adjusted(conn, ["1001.T", "1002.T"])
    assert got["1002.T"].index[-1].date() == days[125]
    assert got["1001.T"]["close"].pct_change().abs().max() < 0.2      # 調整終値は連続
    rep2 = prices.update_prices(conn, ["1001.T", "1002.T"], today, cfg, downloader=FakeDL(server))
    assert rep2["refetched"] == {}


def test_dividend_adjustment_change_triggers_refetch(tmp_path):
    cfg = load_config(overrides={"prices": {"history_days": 100}})
    conn = connect(tmp_path / "p.db")
    days = business_days(date(2026, 1, 5), 100)
    path = make_path(100, 3, 1000)
    insert_prices(conn, price_rows("1001.T", days[:90], path[:90]))
    server = {"1001.T": price_rows("1001.T", days, path, dividend_factor=0.99)}
    rep = prices.update_prices(conn, ["1001.T"], days[-1], cfg, downloader=FakeDL(server))
    assert rep["refetched"] == {"1001.T": "adj_mismatch"}


def test_chunk_failure_retries_one_by_one(tmp_path, cfg):
    conn = connect(tmp_path / "p.db")
    days = business_days(date(2026, 1, 5), 60)
    server = {t: price_rows(t, days, make_path(60, i, 500)) for i, t in enumerate(["1001.T", "1002.T", "1003.T"])}
    rep = prices.update_prices(conn, list(server), days[-1], cfg, downloader=FakeDL(server, fail={"1002.T"}))
    assert len(rep["failed"]) == 1 and rep["failed"][0].startswith("1002.T")
    assert set(prices.load_raw(conn, list(server))) == {"1001.T", "1003.T"}   # 失敗銘柄以外は反映される


def test_needs_full_refetch_ignores_small_diff(cfg):
    days = business_days(date(2026, 1, 5), 10)
    rows = price_rows("X.T", days, np.full(10, 100.0))
    stored = pd.DataFrame(rows).set_index(pd.DatetimeIndex(pd.to_datetime([r["trade_date"] for r in rows])))
    fresh = yf_frame(rows)
    assert prices.needs_full_refetch(stored, fresh, cfg) is None
    fresh2 = fresh.copy()
    fresh2["Close"] = fresh2["Close"] * 1.01     # 1% は許容（しきい値 2%）
    fresh2["Adj Close"] = fresh2["Adj Close"] * 1.01
    assert prices.needs_full_refetch(stored, fresh2, cfg) is None
