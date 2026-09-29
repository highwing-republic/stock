from datetime import date

from synth import business_days

from src.watch import ranking
from src.watch.store import Store

DAYS = [d.isoformat() for d in business_days(date(2026, 9, 7), 10)]
IDX = {d: i for i, d in enumerate(DAYS)}


def daily(spec):
    """spec: {day_index: {code: (score, status)}} → build_daily_scores 互換の day → code → info。"""
    out = {d: {} for d in DAYS}
    for i, items in spec.items():
        for code, (score, status) in items.items():
            out[DAYS[i]][code] = {"score": score, "status": status, "setup": "TREND", "imp": False,
                                  "episode_id": f"{code}-x"}
    return out


def run(cfg, spec, kind="weekly", days=DAYS[:5]):
    eps = {f"{c}-x": {"event_ids": [], "primary_trigger_event_id": None} for items in spec.values() for c in items}
    return ranking.run_period(kind, days, daily(spec), eps, {}, cfg)


def codes(entries):
    return [e["security_code"] for e in entries]


def test_fill_by_score_and_display_order(cfg):
    day0 = {f"100{i}": (50 + i, "WATCH") for i in range(7)}
    entries = run(cfg, {0: day0}, days=DAYS[:1])
    assert codes(entries) == ["1006", "1005", "1004", "1003", "1002"]          # TOP5 のみ
    assert [e["rank"] for e in entries] == [1, 2, 3, 4, 5]


def test_hysteresis_requires_margin_and_one_swap_per_day(cfg):
    base = {f"100{i}": (50 + i, "WATCH") for i in range(5)}          # 1000..1004 → 50..54
    # 外部 2 銘柄: worst=50 に対し +7 では入れ替えない。次の日 +8 で 1 件だけ入れ替え
    d1 = dict(base, **{"2001": (57, "WATCH")})
    d2 = dict(base, **{"2001": (58, "WATCH"), "2002": (60, "WATCH")})
    d3 = dict(base, **{"2001": (59, "WATCH"), "2002": (60, "WATCH")})
    e1 = run(cfg, {0: base, 1: d1}, days=DAYS[:2])
    assert "2001" not in codes(e1)
    e2 = run(cfg, {0: base, 1: d1, 2: d2}, days=DAYS[:3])
    assert "2002" in codes(e2) and "2001" not in codes(e2) and "1000" not in codes(e2)   # 1 日 1 件
    e3 = run(cfg, {0: base, 1: d1, 2: d2, 3: d3}, days=DAYS[:4])
    assert "2001" in codes(e3) and "1001" not in codes(e3)   # 翌日にもう 1 件（59 >= 51 + 8）


def test_removed_members_free_slots_and_reacted_not_new(cfg):
    d0 = {"A": (60, "WATCH"), "B": (59, "WATCH"), "C": (58, "WATCH"), "D": (57, "WATCH"), "E": (56, "WATCH"),
          "F": (55, "WATCH")}
    d1 = {"A": (60, "WATCH"), "C": (58, "WATCH"), "D": (57, "WATCH"), "E": (56, "WATCH"), "F": (55, "WATCH"),
          "G": (70, "REACTED")}                                  # B は BROKEN/CLOSED で消える。REACTED の G は新規に入れない
    entries = run(cfg, {0: d0, 1: d1}, days=DAYS[:2])
    assert codes(entries) == ["A", "C", "D", "E", "F"]
    # 既存メンバーが REACTED に遷移しても残る
    d1b = dict(d0, A=(60, "REACTED"))
    assert "A" in codes(run(cfg, {0: d0, 1: d1b}, days=DAYS[:2]))


def test_monthly_size_and_bonus_cap(cfg):
    spec = {0: {f"M{i:02d}": (50 + i, "WATCH") for i in range(12)}}
    entries = run(cfg, spec, kind="monthly", days=DAYS[:1])
    assert len(entries) == 10 and entries[0]["security_code"] == "M11"


def test_period_keys():
    assert ranking.period_key("weekly", "2026-12-31") == "2026-W53"
    assert ranking.period_key("weekly", "2027-01-01") == "2026-W53"
    assert ranking.period_key("weekly", "2027-01-04") == "2027-W01"
    assert ranking.period_key("monthly", "2026-09-29") == "2026-09"


def test_rank_change():
    prev = [{"security_code": "A", "rank": 1}, {"security_code": "B", "rank": 2}]
    assert ranking.rank_change("A", 2, prev) == "down" and ranking.rank_change("B", 1, prev) == "up"
    assert ranking.rank_change("A", 1, prev) == "same" and ranking.rank_change("Z", 3, prev) == "new"
    assert ranking.rank_change("A", 1, None) == "new"


def test_final_snapshot_is_immutable(tmp_path):
    store = Store(tmp_path)
    snap = ranking.snapshot("weekly", "2026-W36", "2026-09-04", [{"rank": 1, "security_code": "A"}], True)
    assert store.write_ranking("weekly", "2026-W36", snap) is True
    before = store.ranking_path("weekly", "2026-W36").read_bytes()
    other = ranking.snapshot("weekly", "2026-W36", "2026-09-04", [{"rank": 1, "security_code": "B"}], True)
    assert store.write_ranking("weekly", "2026-W36", other) is False
    assert store.ranking_path("weekly", "2026-W36").read_bytes() == before
    # 未凍結なら上書きされる
    live = ranking.snapshot("weekly", "2026-W40", "2026-09-29", [], False)
    assert store.write_ranking("weekly", "2026-W40", live) is True
    assert store.write_ranking("weekly", "2026-W40", dict(live, snapshot_date="2026-09-30")) is True


def ep_and_events(cfg, etype="NEW_HOLDER", tags=None):
    ev = {"event_id": "e1", "event_type": etype, "tags": tags or [etype], "event_market_date": DAYS[0]}
    ep = {"episode_id": "x", "event_ids": ["e1"], "primary_trigger_event_id": "e1"}
    return ep, {"e1": ev}


def test_score_components(cfg):
    ep, evs = ep_and_events(cfg, "CONSECUTIVE_INCREASE", ["CONSECUTIVE_INCREASE", "PURPOSE_CHANGE"])
    tr = {"status": "CONFIRMED", "setup": "BREAKOUT", "range_pos60": 0.95, "imp5": True, "over_now": False}
    # event 35(上限, day0 で減衰なし) + setup 30+5 + confirmation 15 + improvement 10 = 95
    assert ranking.day_score(tr, ep, evs, DAYS[0], IDX, cfg) == 95
    assert ranking.day_score(tr, ep, evs, DAYS[0], IDX, cfg, margin_bonus=True) == 100
    assert ranking.day_score(dict(tr, over_now=True), ep, evs, DAYS[0], IDX, cfg) == 85
    # 減衰: 経過 6 営業日 → 1 - 6/60 = 0.9 → 31.5
    assert ranking.day_score(dict(tr, status="WATCH", setup=None, imp5=False), ep, evs, DAYS[6], IDX, cfg) == 31.5
    ep2, evs2 = ep_and_events(cfg, "INCREASE")
    assert ranking.day_score(dict(tr, status="WATCH", setup="RECOVERY", range_pos60=0.5, imp5=False),
                             ep2, evs2, DAYS[0], IDX, cfg) == 15 + 20
    # 訂正・売却イベントだけの Episode は event 点が 0
    ep3, evs3 = ep_and_events(cfg, "CORRECTION")
    assert ranking.day_score(dict(tr, status="WATCH", setup=None, imp5=False), ep3, evs3, DAYS[0], IDX, cfg) == 0


def test_build_daily_scores_excludes_non_members_and_unranked_states(cfg):
    ep, evs = ep_and_events(cfg)
    eps = {"x": ep, "y": ep}
    mk = lambda st: {DAYS[1]: {"episode_id": "x", "status": st, "setup": "TREND", "range_pos60": 0.5,  # noqa: E731
                               "imp": False, "imp5": False, "over_now": False}}
    traces = {"1001": mk("WATCH"), "1002": mk("BROKEN"), "1003": mk("CANDIDATE"), "1004": mk("WATCH")}
    member = lambda code, d: code != "1004"  # noqa: E731
    out = ranking.build_daily_scores(traces, eps, evs, DAYS, IDX, member, cfg)
    assert set(out[DAYS[1]]) == {"1001"}                       # JPX400 外・BROKEN・CANDIDATE は対象外
