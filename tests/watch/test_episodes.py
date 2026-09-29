from datetime import date

from synth import business_days

from src.watch.episodes import replay_code

DAYS = [d.isoformat() for d in business_days(date(2026, 1, 5), 140)]
IDX = {d: i for i, d in enumerate(DAYS)}


def base_row(**kw):
    row = {"close": 100.0, "prev_close": 100.0, "ma20": 99.0, "ma60": 95.0, "ma200": 90.0, "ma20_slope5": 0.0,
           "range_pos60": 0.5, "dist_52w_high": 0.2, "vol_ratio20": 1.0, "rel20": 0.0, "rel60": 0.0, "ret5": 0.0,
           "primary": None, "breakout": False, "recovery": False, "trend": False, "improved": False,
           "milestone_any": False, "improve_recent": False, "new_high60": False, "ma20_reclaimed": False,
           "ma60_reclaimed": False, "rel20_pos": False, "atr_pct_rank252": 0.5}
    row.update(kw)
    return row


def make_rows(overrides=None, setup_from=None, setup_to=None, skip=()):
    """setup_from..setup_to（日付 index）は TREND 成立。overrides: {index: dict}。"""
    rows = {}
    for i, d in enumerate(DAYS):
        if i in skip:
            continue
        kw = {}
        if setup_from is not None and setup_from <= i <= (setup_to if setup_to is not None else 10**9):
            kw = {"primary": "TREND", "trend": True, "close": 101.0, "ma20": 100.0}
        kw.update((overrides or {}).get(i, {}))
        rows[d] = base_row(**kw)
    return rows


def ev(i, etype="NEW_HOLDER", eid=None, **kw):
    d = DAYS[i]
    e = {"event_id": eid or f"edinet:E{i}", "security_code": "1001", "company_name": "テスト", "filer_name": "A証券",
         "event_type": etype, "tags": [etype], "disclosed_at": f"{d} 10:00", "event_market_date": d,
         "holding_ratio": 6.0, "previous_holding_ratio": 5.0, "holding_change": 1.0, "important_proposal": False}
    e.update(kw)
    return e


def replay(cfg, events, rows, member=lambda c, d: True, bench=None):
    bench = bench or {d: 100.0 for d in DAYS}
    return replay_code("1001", "テスト", events, DAYS, IDX, member, rows, bench, cfg)


def hist(ep):
    return [(h["date"], h["to"], h["reason_code"]) for h in ep["status_history"]]


def test_candidate_to_watch_same_day_and_merge_of_events(cfg):
    events = [ev(10), ev(15, "INCREASE"), ev(16, "CORRECTION"), ev(17, "DECREASE")]
    rows = make_rows(setup_from=5)
    eps, traces = replay(cfg, events, rows)
    assert len(eps) == 1                                  # 複数イベントは 1 Episode に統合
    ep = eps[0]
    assert ep["episode_id"] == f"1001-{DAYS[10]}"
    assert ep["event_ids"] == ["edinet:E10", "edinet:E15", "edinet:E16", "edinet:E17"]
    assert hist(ep)[:2] == [(DAYS[10], "CANDIDATE", "TRIGGER_EVENT"), (DAYS[10], "WATCH", "SETUP_OK")]
    assert ep["initial_price"] == 101.0 and ep["initial_bench"] == 100.0
    assert (DAYS[15], "STRENGTHENING", "INCREASE_EVENT") in hist(ep)   # WATCH 開始後の INCREASE
    assert ep["last_trigger_at"] == DAYS[10]                # INCREASE はトリガーではない
    assert traces[DAYS[10]]["status"] == "WATCH" and traces[DAYS[15]]["status"] == "STRENGTHENING"


def test_correction_alone_never_starts_episode(cfg):
    eps, traces = replay(cfg, [ev(10, "CORRECTION"), ev(11, "INCREASE"), ev(12, "CHANGE_UNKNOWN")],
                         make_rows(setup_from=0))
    assert eps == [] and traces == {}


def test_non_member_trigger_ignored_and_universe_exit(cfg):
    member = lambda c, d: d >= date.fromisoformat(DAYS[20]) and d <= date.fromisoformat(DAYS[40])  # noqa: E731
    eps, _ = replay(cfg, [ev(10)], make_rows(setup_from=0), member)
    assert eps == []                                         # 事象日にメンバーでない
    eps, traces = replay(cfg, [ev(25)], make_rows(setup_from=0), member)
    assert eps[0]["status"] == "CLOSED" and eps[0]["close_reason"] == "UNIVERSE_EXIT"
    assert eps[0]["closed_at"] == DAYS[41] and DAYS[41] not in traces


def test_candidate_expires_without_setup(cfg):
    eps, traces = replay(cfg, [ev(10)], make_rows())
    ep = eps[0]
    assert ep["status"] == "CLOSED" and ep["close_reason"] == "NO_SETUP" and ep["watch_started_at"] is None
    assert ep["closed_at"] == DAYS[10 + cfg.episodes.candidate_expiry_days]
    assert traces[DAYS[10]]["status"] == "CANDIDATE"


def test_candidate_becomes_watch_when_setup_appears_late(cfg):
    eps, _ = replay(cfg, [ev(10)], make_rows(setup_from=18))
    assert eps[0]["watch_started_at"] == DAYS[18]


def test_overextended_blocks_watch_then_allows_after_pullback(cfg):
    rows = make_rows(setup_from=10, overrides={i: {"close": 125.0} for i in range(10, 14)})
    for i in range(10, 14):
        rows[DAYS[i]]["prev_close"] = 100.0
    rows[DAYS[10]]["prev_close"] = 100.0
    eps, _ = replay(cfg, [ev(10)], rows)
    ep = eps[0]
    assert ep["overextended"] is True and ep["base_close"] == 100.0
    assert ep["watch_started_at"] == DAYS[14]                # +25% の間は WATCH にしない


def test_confirmed_reacted_and_expiry_extended_by_new_trigger(cfg):
    o = {30: {"new_high60": True, "vol_ratio20": 1.8, "rel20": 0.02}}
    eps, _ = replay(cfg, [ev(10)], make_rows(o, setup_from=5))
    assert (DAYS[30], "CONFIRMED", "NEW_HIGH60_VOLUME") in hist(eps[0])
    # 出来高が足りなければ CONFIRMED にならない
    o2 = {30: {"new_high60": True, "vol_ratio20": 1.2, "rel20": 0.02}}
    eps2, _ = replay(cfg, [ev(10)], make_rows(o2, setup_from=5))
    assert all(h[1] != "CONFIRMED" for h in hist(eps2[0]))
    # REACTED（+15%）は CONFIRMED からも遷移し、REACTED からは崩れ判定を受けない
    o3 = {30: {"new_high60": True, "vol_ratio20": 1.8, "rel20": 0.02},
          31: {"close": 120.0, "prev_close": 101.0}}
    eps3, _ = replay(cfg, [ev(10)], make_rows(o3, setup_from=5, setup_to=31))
    assert [h[1] for h in hist(eps3[0])][:4] == ["CANDIDATE", "WATCH", "CONFIRMED", "REACTED"]
    # 期限: 60 営業日で EXPIRED。途中で新しいトリガーが来ると、その日から再計算
    eps4, _ = replay(cfg, [ev(10)], make_rows(setup_from=5))
    assert eps4[0]["close_reason"] == "EXPIRED" and eps4[0]["closed_at"] == DAYS[70]
    eps5, _ = replay(cfg, [ev(10), ev(50, "LARGE_INCREASE")], make_rows(setup_from=5))
    assert len(eps5) == 1 and eps5[0]["closed_at"] == DAYS[110]


def test_excess_return_reacted(cfg):
    bench = {d: 100.0 for d in DAYS}
    rows = make_rows(setup_from=5, overrides={40: {"close": 112.0}})
    eps, _ = replay(cfg, [ev(10)], rows, bench=bench)          # +10.9% と超過 +10.9pt(>=10)
    assert (DAYS[40], "REACTED", "EXCESS_THRESHOLD") in hist(eps[0])


def test_broken_by_ma20_streak_then_closed_and_reopen(cfg):
    # 30..32 の 3 日、Setup 不成立かつ 20 日線割れ → 32 で BROKEN、33 で CLOSED、その後の新トリガーで再開
    o = {i: {"primary": None, "trend": False, "close": 97.0, "ma20": 99.0} for i in (30, 31, 32)}
    events = [ev(10), ev(60, "LARGE_INCREASE")]
    eps, traces = replay(cfg, events, make_rows(o, setup_from=5, setup_to=29))
    first = eps[0]
    assert (DAYS[32], "BROKEN", "BELOW_MA20") in hist(first)
    assert first["close_reason"] == "BROKEN" and first["closed_at"] == DAYS[33]
    assert traces[DAYS[32]]["status"] == "BROKEN" and DAYS[33] not in traces
    assert len(eps) == 2 and eps[1]["episode_id"] == f"1001-{DAYS[60]}"     # 再開は別 Episode


def test_two_day_dip_does_not_break_and_ma60_break_is_immediate(cfg):
    o = {i: {"primary": None, "trend": False, "close": 97.0, "ma20": 99.0} for i in (30, 31)}
    eps, _ = replay(cfg, [ev(10)], make_rows(o, setup_from=5))
    assert all(h[1] != "BROKEN" for h in hist(eps[0]))
    o2 = {30: {"close": 91.0, "ma60": 95.0}}          # 95*(1-0.03)=92.15 を下回る
    eps2, _ = replay(cfg, [ev(10)], make_rows(o2, setup_from=5))
    assert (DAYS[30], "BROKEN", "BELOW_MA60") in hist(eps2[0])


def test_missing_price_day_does_not_break_state(cfg):
    eps, traces = replay(cfg, [ev(10)], make_rows(setup_from=5, skip={20, 21}))
    assert eps[0]["status"] == "CLOSED" and eps[0]["close_reason"] == "EXPIRED"
    assert DAYS[20] in traces                       # 価格が無い日も Episode は継続（前回の状態を維持）


def test_rerun_is_deterministic(cfg):
    events = [ev(10), ev(15, "INCREASE"), ev(50, "LARGE_INCREASE")]
    a = replay(cfg, events, make_rows(setup_from=5))
    b = replay(cfg, list(reversed(events)), make_rows(setup_from=5))
    assert a == b
