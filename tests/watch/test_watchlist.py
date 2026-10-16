"""監視ボード用 summary.watchlist（順位なし・監視開始日の新しい順）のテスト。"""
import pytest

from src.watch import export
from test_pipeline_e2e import load, run_pipeline


def _ep(code, status, start, *, history=None, closed_at=None, close_reason=None, events=("e1",)):
    hist = history or [{"date": start, "from": "CANDIDATE", "to": "WATCH", "reason_code": "SETUP_OK"}]
    return {"episode_id": f"{code}-{start}", "security_code": code, "company_name": f"銘柄{code}", "status": status,
            "started_at": start, "watch_started_at": start, "closed_at": closed_at, "close_reason": close_reason,
            "primary_trigger_event_id": events[0] if events else None, "event_ids": list(events),
            "last_trigger_at": start, "setup_type": "BREAKOUT", "setups": ["BREAKOUT"], "overextended": False,
            "initial_price": 100.0, "initial_bench": 1000.0, "base_close": 99.0, "status_history": hist}


def _ctx(cfg, episodes, events):
    days = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08"]
    closes = [98.0, 100.0, 103.0, 105.0, 104.0, 110.0]
    bench = dict(zip(days, [1000.0, 1000.0, 1010.0, 1005.0, 1000.0, 990.0]))
    return export.PublicContext(
        cfg=cfg, now="2026-10-08T21:00:00+09:00", as_of=days[-1], days=days,
        day_index={d: i for i, d in enumerate(days)}, sources={}, universe_count=3,
        names={"1001": "ワン", "1002": "ツー", "1003": "スリー", "1004": "フォー", "1005": "ファイブ"},
        weekly={"key": "k", "entries": [], "previous": None}, monthly={"key": "k", "entries": [], "previous": None},
        episodes=episodes, events=events, setup_changes=[], rows={},
        series=lambda code: (days, closes), bench_close=bench)


def test_watchlist_statuses_order_and_chart(cfg):
    events = [{"event_id": "e1", "security_code": "1001", "event_type": "INCREASE", "filer_name": "A証券",
               "holding_ratio": 7.5, "previous_holding_ratio": 6.5, "disclosed_at": "2026-10-01 10:00",
               "event_market_date": "2026-10-01", "source_url": "u1"},
              {"event_id": "e2", "security_code": "1001", "event_type": "DECREASE", "filer_name": "B",
               "holding_ratio": 5.2, "previous_holding_ratio": 6.0, "disclosed_at": "2026-10-07 10:00",
               "event_market_date": "2026-10-07", "source_url": "u2"},
              {"event_id": "e3", "security_code": "1003", "event_type": "NEW_HOLDER", "filer_name": "C社",
               "holding_ratio": 5.1, "previous_holding_ratio": None, "disclosed_at": "2026-10-05 16:00",
               "event_market_date": "2026-10-06", "source_url": "u3"}]
    eps = [
        _ep("1001", "STRENGTHENING", "2026-10-02", events=("e1", "e2")),
        _ep("1002", "CANDIDATE", "2026-10-07"),                       # まだ形が成立していない → 載せない
        _ep("1003", "WATCH", "2026-10-06", events=("e3",)),
        _ep("1004", "CLOSED", "2026-10-02", closed_at="2026-10-08", close_reason="EXPIRED"),   # 今日の期限切れ → 載せる
        _ep("1005", "CLOSED", "2026-10-02", closed_at="2026-10-07", close_reason="BROKEN"),   # 前日以前の終了 → 載せない
    ]
    wl = export._watchlist(_ctx(cfg, eps, events), {e["event_id"]: e for e in events})
    assert [w["securityCode"] for w in wl] == ["1003", "1004", "1001"]       # 監視開始日の新しい順、同日はコード降順
    assert {w["status"] for w in wl} == {"WATCH", "CLOSED", "STRENGTHENING"}
    w1001 = next(w for w in wl if w["securityCode"] == "1001")
    # 減少報告ではなく、最新の取得・買い増しの報告をきっかけとして示す
    assert w1001["trigger"]["label"] == "買い増し" and w1001["trigger"]["detail"] == "6.5% → 7.5%（A証券）"
    assert w1001["trigger"]["eventCount"] == 2
    assert w1001["daysSinceWatch"] == 4
    chart = w1001["chart"]
    assert chart["stock"][chart["startIndex"]] == 100.0 and chart["market"][chart["startIndex"]] == 100.0
    assert chart["stock"][-1] == 110.0 and len(chart["stock"]) == len(chart["market"]) == 6
    w1003 = next(w for w in wl if w["securityCode"] == "1003")
    assert w1003["trigger"]["label"] == "新規大量保有" and w1003["trigger"]["marketDate"] == "2026-10-06"
    for w in wl:
        assert set(w) == export.WATCHLIST_ITEM_KEYS


def test_watchlist_chart_lead_days_is_configurable(cfg):
    from src.watch.config import load_config
    short = load_config(overrides={"export": {"watchlist_chart_lead_days": 1}})
    eps = [_ep("1001", "WATCH", "2026-10-06", events=())]
    wl = export._watchlist(_ctx(short, eps, []), {})
    assert wl[0]["chart"]["startIndex"] == 1 and len(wl[0]["chart"]["stock"]) == 4
    assert wl[0]["trigger"] is None


def test_pipeline_summary_has_valid_watchlist(env):
    res = run_pipeline(env)
    assert res.status == "ok", res.message
    summary = load(env.out / "summary.json")
    wl = summary["watchlist"]
    assert wl, "合成データでも監視中の銘柄がある"
    starts = [w["watchStartedAt"] for w in wl]
    assert starts == sorted(starts, reverse=True)
    for w in wl:
        assert w["status"] in export.WATCHLIST_STATUSES + ("CLOSED",)
        assert (env.out / "stocks" / f"{w['securityCode']}.json").exists()
        if w["chart"]:
            assert w["chart"]["stock"][w["chart"]["startIndex"]] == 100.0
    # 非メンバー（1004）や形が成立しない銘柄（1006）は載らない
    assert {"1004", "1006"}.isdisjoint({w["securityCode"] for w in wl})


def test_validate_rejects_bad_watchlist():
    base = {"schemaVersion": 1, "updatedAt": "x", "asOfMarketDate": "d", "benchmarkLabel": "b",
            "sources": {"edinet": {}, "prices": {}, "jpx400": {}, "margin": {}},
            "weekly": {"periodKey": "k", "entries": []}, "monthly": {"periodKey": "k", "entries": []},
            "stateChanges": [], "universeCount": 0, "disclaimer": "d",
            "universe": {"key": "jpx400", "label": "JPX日経400", "memberCount": 0, "note": None}}
    item = {"securityCode": "1001", "companyName": "n", "setupType": "TREND", "status": "WATCH",
            "watchStartedAt": "2026-10-01", "daysSinceWatch": 1, "statusChangedAt": "2026-10-01",
            "statusReason": "SETUP_OK", "trigger": None, "returnSinceWatch": 1.0, "excessSinceWatch": 0.5,
            "chart": {"startIndex": 0, "stock": [100.0, 101.0], "market": [100.0, 100.2]}}
    stock = {k: None for k in export.STOCK_KEYS}
    export.validate_public({"summary.json": dict(base, watchlist=[item]), "stocks/1001.json": stock})
    with pytest.raises(export.ExportValidationError, match="missing stock file"):
        export.validate_public({"summary.json": dict(base, watchlist=[item])})
    newer = dict(item, securityCode="1002", watchStartedAt="2026-10-05")
    files = {"summary.json": dict(base, watchlist=[item, newer]), "stocks/1001.json": stock, "stocks/1002.json": stock}
    with pytest.raises(export.ExportValidationError, match="ordered"):
        export.validate_public(files)
    with pytest.raises(export.ExportValidationError, match="chart"):
        export.validate_public({"summary.json": dict(base, watchlist=[dict(item, chart={"startIndex": 5, "stock": [1],
                                                                                       "market": [1]})]),
                                "stocks/1001.json": stock})
    with pytest.raises(export.ExportValidationError, match="keys mismatch"):
        export.validate_public({"summary.json": dict(base, watchlist=[dict(item, rank=1)]), "stocks/1001.json": stock})
