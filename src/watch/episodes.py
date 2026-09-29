"""Watch Episode 状態機械。events・membership・prices・config からの日次リプレイで決定的に算出する。"""
from __future__ import annotations

from datetime import date
from typing import Callable

ACTIVE_STATUSES = ("CANDIDATE", "WATCH", "STRENGTHENING", "CONFIRMED", "REACTED", "BROKEN")
LIVE_STATUSES = ("WATCH", "STRENGTHENING", "CONFIRMED")  # BROKEN 判定の対象
RANKABLE_STATUSES = ("WATCH", "STRENGTHENING", "CONFIRMED", "REACTED")


def _f(value):
    """NaN/None を None に。"""
    if value is None or value != value:
        return None
    return float(value)


def _gt(a, b) -> bool:
    a, b = _f(a), _f(b)
    return a is not None and b is not None and a > b


def _ge(a, b) -> bool:
    a, b = _f(a), _f(b)
    return a is not None and b is not None and a >= b


def replay_code(code: str, company_name: str, events: list[dict], days: list[str], day_index: dict[str, int],
                is_member: Callable[[str, date], bool], rows: dict[str, dict], bench: dict[str, float],
                cfg) -> tuple[list[dict], dict[str, dict]]:
    """1 銘柄を日次リプレイし (episodes, traces) を返す。

    rows: 日付 → 指標＋Setup 列(dict)。bench: 日付 → ベンチマーク調整終値。
    traces: 日付 → その日の引け後に進行中だった Episode のスナップショット。
    """
    ec = cfg.episodes
    triggers = set(cfg.events.trigger_types)
    strengthen = set(cfg.events.strengthen_types)
    evs = sorted((e for e in events if e["security_code"] == code and e["event_market_date"] >= days[0]),
                 key=lambda e: (e["event_market_date"], e["disclosed_at"], e["event_id"]))
    ptr = 0
    episodes: list[dict] = []
    traces: dict[str, dict] = {}
    cur: dict | None = None
    ctx: dict = {}

    def new_episode(day: str, event: dict) -> dict:
        eid = f"{code}-{event['event_market_date']}"
        n = sum(1 for e in episodes if e["episode_id"].startswith(eid))
        if n:
            eid = f"{eid}-{n + 1}"
        ep = {"episode_id": eid, "security_code": code, "company_name": company_name, "status": "CANDIDATE",
              "started_at": day, "watch_started_at": None, "closed_at": None, "close_reason": None,
              "primary_trigger_event_id": event["event_id"], "event_ids": [event["event_id"]],
              "last_trigger_at": day, "setup_type": None, "setups": [], "overextended": False,
              "initial_price": None, "initial_bench": None, "base_close": None,
              "status_history": [{"date": day, "from": None, "to": "CANDIDATE", "reason_code": "TRIGGER_EVENT"}]}
        episodes.append(ep)
        ctx.clear()
        ctx.update({"below": 0, "base_set": False})
        return ep

    def move(ep: dict, day: str, to: str, reason: str) -> None:
        ep["status_history"].append({"date": day, "from": ep["status"], "to": to, "reason_code": reason})
        ep["status"] = to

    def close(ep: dict, day: str, reason: str) -> None:
        move(ep, day, "CLOSED", reason)
        ep["closed_at"], ep["close_reason"] = day, reason

    for d in days:
        i = day_index[d]
        dd = date.fromisoformat(d)
        row = rows.get(d)
        primary = None
        if row is not None:
            primary = row.get("primary") if isinstance(row.get("primary"), str) else None
        if cur is not None and not is_member(code, dd):
            close(cur, d, "UNIVERSE_EXIT")
            cur = None
        new_signal = False
        while ptr < len(evs) and evs[ptr]["event_market_date"] <= d:
            ev = evs[ptr]
            ptr += 1
            etype = ev["event_type"]
            if cur is not None:
                cur["event_ids"].append(ev["event_id"])
                if etype in triggers:
                    cur["last_trigger_at"] = d
                if etype in strengthen and cur["status"] == "WATCH" and d > cur["watch_started_at"]:
                    new_signal = True
            elif etype in triggers and is_member(code, date.fromisoformat(ev["event_market_date"])):
                cur = new_episode(d, ev)
        if cur is None:
            continue
        if row is not None and not ctx["base_set"]:
            cur["base_close"] = _f(row.get("prev_close"))
            ctx["base_set"] = True
        base = cur["base_close"]
        close_now = _f(row["close"]) if row is not None else None
        over_now = bool(base and close_now and close_now / base - 1 >= ec.overextended_return)
        if over_now:
            cur["overextended"] = True
        status = cur["status"]

        # ---- 1 日 1 段階の遷移
        if status == "BROKEN":
            close(cur, d, "BROKEN")
        elif status == "CANDIDATE":
            anchor = day_index[cur["last_trigger_at"]]
            if row is not None and primary and not over_now:
                move(cur, d, "WATCH", "SETUP_OK")
                cur["watch_started_at"] = d
                cur["initial_price"] = close_now
                cur["initial_bench"] = _f(bench.get(d))
            elif i - anchor >= ec.candidate_expiry_days:
                close(cur, d, "NO_SETUP")
        else:
            anchor = day_index[max(cur["watch_started_at"], cur["last_trigger_at"])]
            if i - anchor >= ec.max_watch_days:
                close(cur, d, "EXPIRED")
            elif row is not None:
                ret = None
                if cur["initial_price"] and close_now:
                    ret = close_now / cur["initial_price"] - 1
                excess = None
                if ret is not None and cur["initial_bench"] and bench.get(d):
                    excess = ret - (bench[d] / cur["initial_bench"] - 1)
                below = (not primary) and _gt(row.get("ma20"), close_now)
                if status in LIVE_STATUSES:
                    ctx["below"] = ctx["below"] + 1 if below else 0
                broke_ma60 = status in LIVE_STATUSES and close_now is not None and _f(row.get("ma60")) is not None \
                    and close_now < row["ma60"] * (1 - ec.broken_ma60_buffer)
                if status in LIVE_STATUSES and (ctx["below"] >= ec.broken_days or broke_ma60):
                    move(cur, d, "BROKEN", "BELOW_MA60" if broke_ma60 else "BELOW_MA20")
                elif status in ("WATCH", "STRENGTHENING") and row.get("new_high60") \
                        and _ge(row.get("vol_ratio20"), ec.confirm_volume_ratio) and _gt(row.get("rel20"), 0):
                    move(cur, d, "CONFIRMED", "NEW_HIGH60_VOLUME")
                elif status in LIVE_STATUSES and ((ret is not None and ret >= ec.reacted_return)
                                                  or (excess is not None and excess >= ec.reacted_excess)):
                    move(cur, d, "REACTED", "RETURN_THRESHOLD" if ret is not None and ret >= ec.reacted_return
                         else "EXCESS_THRESHOLD")
                elif status == "WATCH" and d > cur["watch_started_at"] and (
                        new_signal or row.get("improved") or row.get("rel20_pos")):
                    reason = "INCREASE_EVENT" if new_signal else ("SETUP_IMPROVED" if row.get("improved")
                                                                   else "REL20_POSITIVE")
                    move(cur, d, "STRENGTHENING", reason)
        if row is not None and cur["status"] != "CLOSED":
            cur["setup_type"] = primary or cur["setup_type"]
            cur["setups"] = [s for s in ("BREAKOUT", "TREND", "RECOVERY") if row.get(s.lower())]
        if cur["status"] == "CLOSED":
            cur = None
            continue
        traces[d] = {"episode_id": cur["episode_id"], "status": cur["status"], "over_now": over_now,
                     "setup": primary or cur["setup_type"],
                     "range_pos60": _f(row.get("range_pos60")) if row is not None else None,
                     "imp": bool(row.get("improved") or row.get("milestone_any")) if row is not None else False,
                     "imp5": bool(row.get("improve_recent")) if row is not None else False}
    return episodes, traces
