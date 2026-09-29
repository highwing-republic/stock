"""Radar 初登場後の成績とイベント反応。騰落率は % 表記（小数 1 桁）、超過は % ポイント。"""
from __future__ import annotations

import bisect


def pct(new, base) -> float | None:
    if new is None or base is None or new != new or base != base or not base:
        return None
    return round((new / base - 1) * 100, 1)


def _diff(a, b) -> float | None:
    return None if a is None or b is None else round(a - b, 1)


def episode_performance(episode: dict, stock_close: dict[str, float], bench_close: dict[str, float],
                        days: list[str], day_index: dict[str, int], cfg) -> dict | None:
    """WATCH 開始時点（initial_price / initial_bench）からの +N 営業日・現在の成績。"""
    start = episode.get("watch_started_at")
    if not start or not episode.get("initial_price"):
        return None
    last = days[-1]
    cur_stock = stock_close.get(last)
    cur_bench = bench_close.get(last)
    if episode.get("closed_at") and episode["closed_at"] in stock_close:
        cur_stock, cur_bench = stock_close.get(episode["closed_at"]), bench_close.get(episode["closed_at"])
        last = episode["closed_at"]
    ret, bret = pct(cur_stock, episode["initial_price"]), pct(cur_bench, episode.get("initial_bench"))
    out = {"initialDate": start, "initialPrice": round(episode["initial_price"], 1),
           "currentPrice": round(cur_stock, 1) if cur_stock is not None else None,
           "stockReturn": ret, "benchReturn": bret, "excessReturn": _diff(ret, bret)}
    si, li = day_index[start], len(days) - 1
    for h in cfg.performance.horizons:
        target = days[si + h] if si + h <= li else None
        s = pct(stock_close.get(target), episode["initial_price"]) if target else None
        b = pct(bench_close.get(target), episode.get("initial_bench")) if target else None
        out[f"d{h}"] = {"stockReturn": s, "benchReturn": b, "excessReturn": _diff(s, b),
                        "pendingDays": max(0, si + h - li)}
    return out


def event_reaction(event: dict, dates: list[str], close: list[float], bench_close: dict[str, float]) -> dict:
    """base = Day0 前営業日の調整終値。D1 = Day0、D5 = Day0+4、D20 = Day0+19。"""
    i0 = bisect.bisect_left(dates, event["event_market_date"])
    res = {"eventId": event["event_id"], "d1": None, "d5": None, "d20": None, "excess5": None}
    if i0 == 0 or i0 >= len(dates):
        return res
    base = close[i0 - 1]
    for key, off in (("d1", 0), ("d5", 4), ("d20", 19)):
        if i0 + off < len(dates):
            res[key] = pct(close[i0 + off], base)
    if res["d5"] is not None:
        b0, b5 = bench_close.get(dates[i0 - 1]), bench_close.get(dates[i0 + 4])
        bret = pct(b5, b0)
        res["excess5"] = _diff(res["d5"], bret)
    return res
