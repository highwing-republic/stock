"""日本語の注目理由（最大 4 件）。予測・推奨表現は使わない。"""
from __future__ import annotations

FORBIDDEN_WORDS = ("上昇確率", "買い推奨", "推奨", "強気度")
MAX_REASONS = 4
SETUP_LABELS = {"BREAKOUT": "ブレイクアウト", "RECOVERY": "リカバリー", "TREND": "トレンド"}


def num(x: float, digits: int = 1) -> str:
    text = f"{x:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def event_reason(ev: dict) -> str | None:
    t = ev["event_type"]
    ratio, prev = ev.get("holding_ratio"), ev.get("previous_holding_ratio")
    if t == "NEW_HOLDER":
        body = f"新規大量保有 {num(ratio)}%" if ratio is not None else "新規大量保有"
        return f"{body}（{ev['filer_name']}）" if ev.get("filer_name") else body
    if t in ("INCREASE", "LARGE_INCREASE", "CONSECUTIVE_INCREASE"):
        chg = ev.get("holding_change")
        text = f"{num(chg)}pt買い増し" if chg is not None else "買い増し"
        if ratio is not None and prev is not None:
            text += f" {num(prev)}%→{num(ratio)}%"
        return text
    return None


def build_reasons(episode_events: list[dict], row: dict | None, status: str | None, margin: dict | None,
                  cfg) -> list[str]:
    """episode_events: その Episode の正規化イベント。row: 直近日の指標＋Setup 列。"""
    reasons: list[str] = []
    scored = [e for e in episode_events if e["event_type"] in cfg.ranking.event_points]
    if scored:
        best = max(scored, key=lambda e: (cfg.ranking.event_points[e["event_type"]], e["event_market_date"],
                                          e["event_id"]))
        text = event_reason(best)
        if text:
            reasons.append(text)
        if best["event_type"] == "CONSECUTIVE_INCREASE" or "CONSECUTIVE_INCREASE" in best.get("tags", []):
            reasons.append(f"{cfg.events.consecutive_window_days}日以内に2回買い増し")
    if row:
        def val(key):
            v = row.get(key)
            return None if v is None or v != v else float(v)

        if row.get("new_high60") and (val("vol_ratio20") or 0) >= cfg.episodes.confirm_volume_ratio:
            reasons.append(f"出来高 {num(val('vol_ratio20'))}倍で60日高値更新")
        rp = val("range_pos60")
        if rp is not None and rp >= cfg.price_setup.breakout.range_pos60_min:
            reasons.append(f"60日レンジ上位{round((1 - rp) * 100):d}%")
        if row.get("ma20_reclaimed"):
            reasons.append("20日線を回復")
        elif (val("ma20_slope5") or 0) > 0 and (val("close") or 0) >= (val("ma20") or 1e18):
            reasons.append("20日線上向き")
        rel = val("rel20")
        if rel is not None and rel > 0:
            reasons.append(f"TOPIX連動ETF比 +{num(rel * 100)}pt（20日）")
        dist = val("dist_52w_high")
        if dist is not None and 0 < dist <= cfg.price_setup.trend.dist_52w_high_max:
            reasons.append(f"52週高値まで{round(dist * 100):d}%")
    if margin and margin.get("buyChangePct") is not None and margin["buyChangePct"] < 0:
        reasons.append(f"信用買残 {num(margin['buyChangePct'], 0)}%")
    return reasons[:MAX_REASONS]
