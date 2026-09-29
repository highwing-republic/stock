"""テスト用の合成データ（価格・filings・membership）。ネットワークには触れない。"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np


def business_days(start: date, n: int, holidays=()) -> list[date]:
    out, cur = [], start
    while len(out) < n:
        if cur.weekday() < 5 and cur not in holidays:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def make_path(n: int, seed: int, base=1000.0, drift=0.0004, vol=0.012, shape=None) -> np.ndarray:
    """調整済み終値パス。shape=(index, factor) のリストで区間ごとに上乗せ変動を入れられる。"""
    rng = np.random.RandomState(seed)
    rets = rng.normal(drift, vol, n)
    for start, stop, add in shape or []:
        rets[start:stop] += add
    return base * np.cumprod(1 + rets)


def price_rows(ticker: str, days: list[date], adj: np.ndarray, *, seed=0, split_at: int | None = None,
               skip: set[int] | None = None, vol_base=100000, vol_spikes: dict[int, float] | None = None,
               dividend_factor: float = 1.0) -> list[dict]:
    """adj: 調整済み終値。split_at を指定すると、それ以前の生の値段を 2 倍（2:1 分割）にして格納する。"""
    rng = np.random.RandomState(seed + 99)
    rows = []
    for i, (d, a) in enumerate(zip(days, adj)):
        if skip and i in skip:
            continue
        k = 2.0 if (split_at is not None and i < split_at) else 1.0
        raw_close = a * k
        hi = raw_close * (1 + abs(rng.normal(0, 0.005)))
        lo = raw_close * (1 - abs(rng.normal(0, 0.005)))
        volume = int(vol_base * (vol_spikes or {}).get(i, 1.0) * (1 + rng.uniform(-0.2, 0.2)))
        rows.append({"ticker": ticker, "trade_date": d.isoformat(), "open": raw_close, "high": hi, "low": lo,
                     "close": raw_close, "adj_close": a * dividend_factor, "volume": volume})
    return rows


def insert_prices(conn, rows: list[dict]) -> None:
    conn.executemany("INSERT OR REPLACE INTO prices VALUES(:ticker,:trade_date,:open,:high,:low,:close,:adj_close,:volume)",
                     rows)
    conn.commit()


def insert_filing(conn, doc_id, submit_datetime, code, *, report_type="change", filer="野村證券", ratio=None,
                  prev=None, name=None, important=0, desc=None):
    change = None if ratio is None or prev is None else round(ratio - prev, 4)
    conn.execute("""INSERT OR REPLACE INTO filings(doc_id,submit_datetime,report_type,filer_name,issuer_name,
        security_code,ticker,holding_ratio,previous_holding_ratio,holding_change,important_proposal,doc_description)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                 (doc_id, submit_datetime, report_type, filer, name or f"銘柄{code}", code, f"{code}.T", ratio,
                  prev, change, important, desc or "変更報告書"))
    conn.commit()


def membership(codes: dict[str, str], *, effective_from="2026-01-05", checked="2026-09-29T20:00:00+09:00",
               extra=()) -> dict:
    members = [{"security_code": c, "company_name": n, "effective_from": effective_from, "effective_to": None,
                "source_url": "test", "source_published_at": None, "seeded": True} for c, n in codes.items()]
    members.extend(extra)
    return {"schema_version": 1, "last_checked_at": checked, "last_source_sha256": "x", "last_as_of": effective_from,
            "members": members}
