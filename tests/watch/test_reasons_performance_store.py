import json
from datetime import date

from synth import business_days

from src.watch import performance
from src.watch.reasons import FORBIDDEN_WORDS, MAX_REASONS, build_reasons, num
from src.watch.store import Store, dumps, write_json


def ev(etype, **kw):
    e = {"event_id": "edinet:X", "event_type": etype, "tags": [etype], "filer_name": "野村證券",
         "holding_ratio": 6.4, "previous_holding_ratio": None, "holding_change": None,
         "event_market_date": "2026-09-01"}
    e.update(kw)
    return e


ROW = {"close": 110.0, "ma20": 105.0, "ma20_slope5": 0.01, "range_pos60": 0.88, "rel20": 0.042,
       "dist_52w_high": 0.06, "vol_ratio20": 1.8, "new_high60": True, "ma20_reclaimed": False}


def test_reason_formats(cfg):
    assert num(2.0) == "2" and num(5.10) == "5.1" and num(-14.0, 0) == "-14"
    r = build_reasons([ev("NEW_HOLDER")], ROW, "WATCH", None, cfg)
    assert r[0] == "新規大量保有 6.4%（野村證券）"
    r = build_reasons([ev("LARGE_INCREASE", previous_holding_ratio=5.1, holding_ratio=7.1, holding_change=2.0)],
                      dict(ROW, new_high60=False), "WATCH", {"buyChangePct": -14.0}, cfg)
    assert r[0] == "2pt買い増し 5.1%→7.1%"
    assert "60日レンジ上位12%" in r and "20日線上向き" in r
    assert len(r) <= MAX_REASONS


def test_reasons_capped_and_consecutive_and_margin(cfg):
    e = ev("CONSECUTIVE_INCREASE", previous_holding_ratio=5.0, holding_ratio=6.5, holding_change=1.5)
    r = build_reasons([e], ROW, "CONFIRMED", {"buyChangePct": -14.0}, cfg)
    assert len(r) == 4 and r[1] == "30日以内に2回買い増し"
    row = dict(ROW, new_high60=False, range_pos60=0.5, ma20_slope5=-0.1, rel20=-0.1, dist_52w_high=0.5)
    r = build_reasons([e], row, "WATCH", {"buyChangePct": -14.0}, cfg)
    assert r[-1] == "信用買残 -14%"
    assert build_reasons([e], row, "WATCH", {"buyChangePct": 5.0}, cfg)[-1] != "信用買残 5%"
    assert build_reasons([], None, None, None, cfg) == []


def test_reasons_never_contain_forbidden_words(cfg):
    events = [ev(t, previous_holding_ratio=5.0, holding_change=1.5) for t in
              ("NEW_HOLDER", "LARGE_INCREASE", "CONSECUTIVE_INCREASE", "INCREASE")]
    for e in events:
        for row in (ROW, dict(ROW, ma20_reclaimed=True)):
            for reason in build_reasons([e], row, "CONFIRMED", {"buyChangePct": -20.0}, cfg):
                assert not any(w in reason for w in FORBIDDEN_WORDS)


DAYS = [d.isoformat() for d in business_days(date(2026, 8, 3), 70)]
IDX = {d: i for i, d in enumerate(DAYS)}


def test_episode_performance_horizons_and_pending(cfg):
    stock = {d: 100.0 + i for i, d in enumerate(DAYS)}
    bench = {d: 200.0 + i for i, d in enumerate(DAYS)}
    ep = {"watch_started_at": DAYS[10], "initial_price": stock[DAYS[10]], "initial_bench": bench[DAYS[10]],
          "closed_at": None}
    days = DAYS[:26]                                    # 開始から 15 営業日経過
    perf = performance.episode_performance(ep, stock, bench, days, IDX, cfg)
    assert perf["initialDate"] == DAYS[10] and perf["currentPrice"] == 125.0
    assert perf["stockReturn"] == round((125 / 110 - 1) * 100, 1) == 13.6
    assert perf["benchReturn"] == round((225 / 210 - 1) * 100, 1)
    assert perf["excessReturn"] == round(13.6 - perf["benchReturn"], 1)
    assert perf["d5"]["stockReturn"] == round((115 / 110 - 1) * 100, 1) and perf["d5"]["pendingDays"] == 0
    assert perf["d20"]["stockReturn"] is None and perf["d20"]["pendingDays"] == 5
    assert perf["d60"]["pendingDays"] == 45
    assert performance.episode_performance({"watch_started_at": None}, stock, bench, days, IDX, cfg) is None


def test_event_reaction_base_is_prior_close_and_handles_missing():
    dates = DAYS[:30]
    close = [100.0 + i for i in range(30)]
    bench = {d: 200.0 for d in DAYS}
    r = performance.event_reaction({"event_id": "e", "event_market_date": DAYS[5]}, dates, close, bench)
    assert r["d1"] == round((105 / 104 - 1) * 100, 1)                 # base = Day0 前営業日の終値
    assert r["d5"] == round((109 / 104 - 1) * 100, 1) and r["excess5"] == r["d5"]
    assert r["d20"] == round((124 / 104 - 1) * 100, 1)
    r2 = performance.event_reaction({"event_id": "e", "event_market_date": DAYS[28]}, dates, close, bench)
    assert r2["d1"] is not None and r2["d5"] is None and r2["d20"] is None
    r3 = performance.event_reaction({"event_id": "e", "event_market_date": DAYS[0]}, dates, close, bench)
    assert r3["d1"] is None                                            # 前営業日の価格が無い


def test_store_is_atomic_and_deterministic(tmp_path):
    path = tmp_path / "a" / "x.json"
    assert write_json(path, {"b": 1, "a": "日本語"}) is True
    assert path.read_text(encoding="utf-8") == '{\n "a": "日本語",\n "b": 1\n}\n'
    assert write_json(path, {"a": "日本語", "b": 1}) is False           # 同一内容なら書き換えない
    assert not list(path.parent.glob("*.tmp"))
    assert dumps({"z": [1, 2]}).endswith("\n")


def test_store_job_runs_and_last_success(tmp_path):
    s = Store(tmp_path)
    s.append_job_runs("2026-09", [{"source": "prices", "status": "ok", "finished_at": "t1"}])
    s.append_job_runs("2026-09", [{"source": "prices", "status": "failed", "finished_at": "t2"},
                                  {"source": "margin", "status": "failed", "finished_at": "t2"}])
    assert s.last_success("prices") == "t1" and s.last_success("margin") is None
    s.append_job_runs("2026-10", [{"source": "prices", "status": "partial", "finished_at": "t3"}])
    assert s.last_success("prices") == "t3"
    assert len(json.loads((tmp_path / "job_runs" / "2026-09.json").read_text(encoding="utf-8"))) == 3


def test_store_events_and_setup_history_layout(tmp_path):
    s = Store(tmp_path)
    s.write_events({"2026-09": [{"event_id": "a"}]})
    s.write_setup_history([{"date": "2026-09-02", "security_code": "1", "kind": "SETUP"},
                           {"date": "2026-09-01", "security_code": "2", "kind": "SETUP"}])
    changes = json.loads((tmp_path / "setup_history" / "2026-09.json").read_text(encoding="utf-8"))["changes"]
    assert [c["date"] for c in changes] == ["2026-09-01", "2026-09-02"]
    assert (tmp_path / "events" / "2026-09.json").exists()
