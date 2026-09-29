"""Price Setup（BREAKOUT / RECOVERY / TREND）判定とマイルストーン。"""
from __future__ import annotations

import numpy as np
import pandas as pd

SETUPS = ("BREAKOUT", "RECOVERY", "TREND")
MILESTONES = ("MA20_RECLAIMED", "MA60_RECLAIMED", "NEW_HIGH60", "REL20_TURNED_POSITIVE")
MILESTONE_COLUMNS = {"MA20_RECLAIMED": "ma20_reclaimed", "MA60_RECLAIMED": "ma60_reclaimed",
                     "NEW_HIGH60": "new_high60", "REL20_TURNED_POSITIVE": "rel20_pos"}


def evaluate_setups(ind: pd.DataFrame, cfg) -> pd.DataFrame:
    """指標 DataFrame に Setup 判定列を足して返す。履歴不足の日は Setup が成立しない。"""
    s = cfg.price_setup
    out = ind.copy()
    close = out["close"]
    ok = out["hist_ok"]
    surge_ok = out["ret5"] < s.surge_5d_max
    up20 = (close >= out["ma20"]) & (out["ma20_slope5"] >= 0)
    out["breakout"] = ok & up20 & (out["range_pos60"] >= s.breakout.range_pos60_min) \
        & (out["atr_pct_rank252"] <= s.breakout.atr_pct_rank252_max) & surge_ok
    out["recovery"] = ok & (out["max_drawdown_120"] >= s.recovery.max_drawdown_120_min) \
        & (out["days_since_low60"] >= s.recovery.days_since_low60_min) & (close >= out["ma20"]) \
        & (out["ma20_slope5"] > 0) & (out["rel20"] > out["rel20"].shift(10))
    out["trend"] = ok & (close > out["ma20"]) & (out["ma20"] > out["ma60"]) & (out["ma60_slope10"] > 0) \
        & (out["rel60"] > 0) & (out["dist_52w_high"] <= s.trend.dist_52w_high_max) & surge_ok
    for name in SETUPS:
        out[name.lower()] = out[name.lower()].fillna(False).astype(bool)
    primary = pd.Series(None, index=out.index, dtype=object)
    for name in reversed(list(s.priority)):
        primary = primary.mask(out[name.lower()], name)
    out["primary"] = primary.astype(object).where(primary.notna(), None)
    out["insufficient"] = ~ok.astype(bool)

    prev_close, prev_ma20, prev_ma60 = close.shift(1), out["ma20"].shift(1), out["ma60"].shift(1)
    out["ma20_reclaimed"] = ((close > out["ma20"]) & (prev_close <= prev_ma20)).fillna(False)
    out["ma60_reclaimed"] = ((close > out["ma60"]) & (prev_close <= prev_ma60)).fillna(False)
    out["new_high60"] = (close > out["prev_high60"]).fillna(False)
    out["rel20_pos"] = ((out["rel20"] > 0) & (out["rel20"].shift(1) <= 0)).fillna(False)
    out["milestone_any"] = out[list(MILESTONE_COLUMNS.values())].any(axis=1)
    prev_primary = primary.shift(1)
    first = pd.Series(np.arange(len(out)) == 0, index=out.index)
    improved = (prev_primary.isna() & primary.notna()) | ((prev_primary == "RECOVERY") & primary.isin(["TREND", "BREAKOUT"]))
    out["improved"] = (improved & ~first).astype(bool)
    out["improve_recent"] = (out["improved"] | out["milestone_any"]).astype(float) \
        .rolling(cfg.ranking.improvement_days, min_periods=1).max().astype(bool)
    return out


def active_setups(row) -> list[str]:
    return [name for name in SETUPS if bool(row.get(name.lower()))]


def setup_changes(code: str, sf: pd.DataFrame, days: set[str] | None = None) -> list[dict]:
    """主 Setup の変化とマイルストーンを変化日のみ記録する。days があればその日付のみ。"""
    if len(sf) == 0:
        return []
    dates = sf.index.strftime("%Y-%m-%d")
    cur = [v if isinstance(v, str) else None for v in sf["primary"].tolist()]
    ms = {name: sf[col].to_numpy() for name, col in MILESTONE_COLUMNS.items()}
    out = []
    for i, d in enumerate(dates):
        if days is not None and d not in days:
            continue
        if i > 0 and cur[i] != cur[i - 1]:
            out.append({"date": d, "security_code": code, "kind": "SETUP", "from": cur[i - 1], "to": cur[i]})
        for name in MILESTONES:
            if ms[name][i]:
                out.append({"date": d, "security_code": code, "kind": "MILESTONE", "milestone": name,
                            "to": cur[i]})
    return out
