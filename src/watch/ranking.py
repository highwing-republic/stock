"""Priority Score（内部専用）と週次 TOP5／月次 TOP10（ヒステリシス付き日次リプレイ）。"""
from __future__ import annotations

from datetime import date
from typing import Callable

from .episodes import RANKABLE_STATUSES
from .events import INCREASE_FAMILY

SCORED_EVENT_TYPES = ("NEW_HOLDER", "LARGE_INCREASE", "CONSECUTIVE_INCREASE", "INCREASE")


def period_key(kind: str, day: str) -> str:
    if kind == "weekly":
        y, w, _ = date.fromisoformat(day).isocalendar()
        return f"{y}-W{w:02d}"
    return day[:7]


def event_points(event: dict, cfg) -> float:
    r = cfg.ranking
    base = r.event_points.get(event["event_type"], 0)
    if not base:
        return 0.0
    if "PURPOSE_CHANGE" in event.get("tags", []):
        base += r.purpose_change_bonus
    return float(min(r.event_cap, base))


def day_score(trace: dict, episode: dict, events_by_id: dict, day: str, day_index: dict, cfg,
              margin_bonus: bool = False) -> float:
    r = cfg.ranking
    best = 0.0
    for eid in episode["event_ids"]:
        ev = events_by_id.get(eid)
        if ev is None or ev["event_market_date"] > day:
            continue
        age = day_index[day] - day_index.get(ev["event_market_date"], day_index[day])
        decay = max(r.event_decay_floor, 1 - age / r.event_decay_days)
        best = max(best, event_points(ev, cfg) * decay)
    event = min(r.event_cap, best)
    setup = 0.0
    if trace.get("setup") in r.setup_points:
        setup = r.setup_points[trace["setup"]]
        if (trace.get("range_pos60") or 0) >= r.setup_top_range_pos:
            setup += r.setup_top_bonus
    setup = min(r.setup_cap, setup)
    confirmation = r.status_points.get(trace["status"], 0)
    improvement = r.improvement_points if trace.get("imp5") else 0
    margin = r.margin_points if margin_bonus else 0
    penalty = r.overextended_penalty if trace.get("over_now") else 0
    return round(max(0.0, event + setup + confirmation + improvement + margin - penalty), 2)


def build_daily_scores(traces_by_code: dict[str, dict[str, dict]], episodes_by_id: dict, events_by_id: dict,
                       days: list[str], day_index: dict, is_member: Callable[[str, date], bool], cfg,
                       margin_bonus_fn: Callable[[str, str], bool] | None = None) -> dict[str, dict[str, dict]]:
    """day → code → {score, status, setup, imp, extra_events(この日までの月内追加イベント用)}。
    ランキング対象（RANKABLE かつ当日メンバー）のみ。"""
    out: dict[str, dict[str, dict]] = {d: {} for d in days}
    for code, traces in traces_by_code.items():
        for d, tr in traces.items():
            if tr["status"] not in RANKABLE_STATUSES or not is_member(code, date.fromisoformat(d)):
                continue
            ep = episodes_by_id[tr["episode_id"]]
            bonus = bool(margin_bonus_fn and margin_bonus_fn(code, d))
            out[d][code] = {"score": day_score(tr, ep, events_by_id, d, day_index, cfg, bonus),
                            "status": tr["status"], "setup": tr["setup"], "imp": tr["imp"],
                            "episode_id": tr["episode_id"]}
    return out


def _monthly_adjust(code: str, d: str, info: dict, episodes_by_id: dict, events_by_id: dict,
                    month: str, imp_seen: dict, cfg) -> float:
    r = cfg.ranking
    ep = episodes_by_id[info["episode_id"]]
    extra = 0
    for eid in ep["event_ids"]:
        ev = events_by_id.get(eid)
        if ev is None or eid == ep["primary_trigger_event_id"]:
            continue
        if ev["event_type"] in INCREASE_FAMILY and ev["event_market_date"][:7] == month \
                and ev["event_market_date"] <= d:
            extra += 1
    improved = 1 if imp_seen.get(code) else 0
    return min(r.monthly_cap, info["score"] + r.monthly_event_bonus * extra + r.monthly_improve_bonus * improved)


def run_period(kind: str, period_days: list[str], daily: dict[str, dict[str, dict]], episodes_by_id: dict,
               events_by_id: dict, cfg) -> list[dict]:
    """period_days を先頭から日次リプレイし、最終日の TOP を返す（{rank, security_code, priority_score, status, setup_type}）。"""
    size = cfg.ranking.weekly_size if kind == "weekly" else cfg.ranking.monthly_size
    hyst = cfg.ranking.weekly_hysteresis if kind == "weekly" else cfg.ranking.monthly_hysteresis
    members: list[str] = []
    imp_seen: dict[str, bool] = {}
    scores: dict[str, float] = {}
    infos: dict[str, dict] = {}
    for d in period_days:
        today = daily.get(d, {})
        scores, infos = {}, {}
        for code, info in sorted(today.items()):
            if info["imp"]:
                imp_seen[code] = True
            s = info["score"]
            if kind == "monthly":
                s = _monthly_adjust(code, d, info, episodes_by_id, events_by_id, d[:7], imp_seen, cfg)
            scores[code], infos[code] = s, info
        members = [c for c in members if c in scores]  # BROKEN/CLOSED/非メンバーは外れる
        pool = sorted((c for c in scores if c not in members and infos[c]["status"] != "REACTED"),
                      key=lambda c: (-scores[c], c))
        while len(members) < size and pool:
            members.append(pool.pop(0))
        if members and pool:
            worst = min(members, key=lambda c: (scores[c], c))
            if scores[pool[0]] >= scores[worst] + hyst:
                members[members.index(worst)] = pool[0]
    ordered = sorted(members, key=lambda c: (-scores[c], c))
    return [{"rank": i + 1, "security_code": c, "priority_score": scores[c], "status": infos[c]["status"],
             "setup_type": infos[c]["setup"]} for i, c in enumerate(ordered)]


def rank_change(code: str, rank: int, previous: list[dict] | None) -> str:
    if not previous:
        return "new"
    prev = next((e["rank"] for e in previous if e["security_code"] == code), None)
    if prev is None:
        return "new"
    return "up" if rank < prev else ("down" if rank > prev else "same")


def snapshot(kind: str, key: str, snapshot_date: str, entries: list[dict], final: bool) -> dict:
    return {"schema_version": 1, "period_type": kind, "period_key": key, "snapshot_date": snapshot_date,
            "final": final, "entries": entries}
