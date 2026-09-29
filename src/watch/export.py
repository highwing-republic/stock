"""公開 JSON（public/data/watch/）の生成・検証・置換。スコアは一切出さない。"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from . import performance
from .ranking import rank_change
from .reasons import FORBIDDEN_WORDS, MAX_REASONS, build_reasons
from .setups import MILESTONES
from .store import dumps

SCHEMA_VERSION = 1
BENCHMARK_LABEL = "TOPIX連動ETF（1306）"
STATUS_TO_CHANGE = {"WATCH": "NEW_WATCH", "STRENGTHENING": "STRENGTHENING", "CONFIRMED": "BREAKOUT_CONFIRMED",
                    "REACTED": "REACTED", "BROKEN": "SETUP_BROKEN", "CLOSED": "CLOSED"}
REASON_TEXT = {
    "TRIGGER_EVENT": "大量保有報告を確認", "SETUP_OK": "Price Setupが成立",
    "INCREASE_EVENT": "大量保有の買い増しを確認", "SETUP_IMPROVED": "Price Setupが改善",
    "REL20_POSITIVE": "TOPIX連動ETFを上回りに転換", "NEW_HIGH60_VOLUME": "60日高値を出来高増で更新",
    "RETURN_THRESHOLD": "Watch開始後の値上がりが大きくなった", "EXCESS_THRESHOLD": "TOPIX連動ETFとの差が大きくなった",
    "BELOW_MA20": "20日線を連続で下回った", "BELOW_MA60": "60日線を下回った", "BROKEN": "Price Setupが崩れた",
    "NO_SETUP": "期限内にPrice Setupが成立しなかった", "EXPIRED": "観察期限に到達",
    "UNIVERSE_EXIT": "JPX400から除外された"}
EVENT_LABEL = {"NEW_HOLDER": "新規大量保有", "LARGE_INCREASE": "大幅な買い増し", "CONSECUTIVE_INCREASE": "連続の買い増し",
               "INCREASE": "買い増し", "DECREASE": "保有割合の減少", "CORRECTION": "訂正報告",
               "CHANGE_UNKNOWN": "変更報告（増減不明）"}
MILESTONE_LABEL = {"MA20_RECLAIMED": "20日線を回復", "MA60_RECLAIMED": "60日線を回復",
                   "NEW_HIGH60": "60日高値を更新", "REL20_TURNED_POSITIVE": "TOPIX連動ETFを上回りに転換"}
SUMMARY_KEYS = {"schemaVersion", "updatedAt", "asOfMarketDate", "benchmarkLabel", "sources", "weekly", "monthly",
                "stateChanges", "universeCount", "disclaimer"}
ENTRY_KEYS = {"rank", "securityCode", "companyName", "setupType", "status", "reasons", "watchStartedAt",
              "returnSinceWatch", "excessSinceWatch", "rankChange"}
STOCK_KEYS = {"schemaVersion", "updatedAt", "company", "episode", "pastEpisodes", "reasons", "timeline",
              "priceSetup", "margin", "performance", "eventReactions"}
FORBIDDEN_KEY_PARTS = ("score", "priority")
_AI = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")


class ExportValidationError(Exception):
    pass


@dataclass
class PublicContext:
    cfg: Any
    now: str
    as_of: str                       # asOfMarketDate（最終営業日）
    days: list[str]
    day_index: dict[str, int]
    sources: dict
    universe_count: int
    names: dict[str, str]
    weekly: dict                     # {"key", "entries", "previous"}
    monthly: dict
    episodes: list[dict]
    events: list[dict]
    setup_changes: list[dict]
    rows: dict[str, dict[str, dict]]                    # code → date → 指標行
    series: Callable[[str], tuple[list[str], list[float]]]  # code → (dates, adj close)
    bench_close: dict[str, float]
    margin_views: dict[str, dict] = field(default_factory=dict)


def _r1(x):
    return None if x is None or x != x else round(float(x), 1)


def _num_or_none(x, digits=2):
    return None if x is None or x != x else round(float(x), digits)


def _last_row(ctx: PublicContext, code: str) -> dict | None:
    rows = ctx.rows.get(code) or {}
    for d in reversed(ctx.days):
        if d in rows:
            return rows[d]
    return None


def _episode_for(ctx: PublicContext, code: str, prefer_active=True) -> dict | None:
    eps = [e for e in ctx.episodes if e["security_code"] == code]
    if not eps:
        return None
    active = [e for e in eps if e["status"] != "CLOSED"]
    pool = active if (active and prefer_active) else eps
    return sorted(pool, key=lambda e: (e["started_at"], e["episode_id"]))[-1]


def _perf(ctx: PublicContext, ep: dict) -> dict | None:
    dates, close = ctx.series(ep["security_code"])
    return performance.episode_performance(ep, dict(zip(dates, close)), ctx.bench_close, ctx.days,
                                           ctx.day_index, ctx.cfg)


def _reasons(ctx: PublicContext, ep: dict, events_by_id: dict) -> list[str]:
    evs = [events_by_id[i] for i in ep["event_ids"] if i in events_by_id]
    return build_reasons(evs, _last_row(ctx, ep["security_code"]), ep["status"],
                         ctx.margin_views.get(ep["security_code"]), ctx.cfg)


def _entry(ctx: PublicContext, e: dict, previous: list[dict] | None, events_by_id: dict) -> dict:
    code = e["security_code"]
    ep = _episode_for(ctx, code)
    perf = _perf(ctx, ep) if ep else None
    return {"rank": e["rank"], "securityCode": code, "companyName": ctx.names.get(code, code),
            "setupType": e["setup_type"], "status": e["status"],
            "reasons": _reasons(ctx, ep, events_by_id) if ep else [],
            "watchStartedAt": ep["watch_started_at"] if ep else None,
            "returnSinceWatch": perf["stockReturn"] if perf else None,
            "excessSinceWatch": perf["excessReturn"] if perf else None,
            "rankChange": rank_change(code, e["rank"], previous)}


def _state_changes(ctx: PublicContext) -> list[dict]:
    cutoff = (date.fromisoformat(ctx.as_of) - timedelta(days=ctx.cfg.export.state_change_days)).isoformat()
    out = []
    for ep in ctx.episodes:
        for h in ep["status_history"]:
            change = STATUS_TO_CHANGE.get(h["to"])
            if change and h["date"] >= cutoff:
                out.append({"date": h["date"], "securityCode": ep["security_code"],
                            "companyName": ctx.names.get(ep["security_code"], ep["security_code"]),
                            "change": change, "setupType": ep["setup_type"],
                            "reason": REASON_TEXT.get(h["reason_code"], "")})
    out.sort(key=lambda c: (c["date"], c["securityCode"], c["change"]), reverse=True)
    return out[:ctx.cfg.export.state_change_limit]


def _price_setup(ctx: PublicContext, code: str) -> dict | None:
    row = _last_row(ctx, code)
    if not row or row.get("close") is None:
        return None
    ex = ctx.cfg.export

    def v(k):
        x = row.get(k)
        return None if x is None or x != x else float(x)

    close = v("close")
    labels = []
    if v("ma20") is not None and close >= v("ma20"):
        labels.append("20日線の上")
    if v("range_pos60") is not None and v("range_pos60") >= ctx.cfg.price_setup.breakout.range_pos60_min:
        labels.append(f"60日レンジ上位{round((1 - v('range_pos60')) * 100):d}%")
    if (v("ma20_slope5") or 0) > 0:
        labels.append("20日線が上向き")
    if v("ma60") is not None and close >= v("ma60"):
        labels.append("60日線の上")
    rank = v("atr_pct_rank252")
    atr = None if rank is None else ("低ボラ" if rank <= ex.atr_low_rank else ("高ボラ" if rank >= ex.atr_high_rank else "通常"))
    return {"asOf": ctx.as_of, "labels": labels, "ma20": _r1(v("ma20")), "ma60": _r1(v("ma60")),
            "ma200": _r1(v("ma200")), "rangePos60": _num_or_none(v("range_pos60")),
            "distFrom52wHigh": _num_or_none(v("dist_52w_high")), "atrState": atr,
            "rel20": _r1(None if v("rel20") is None else v("rel20") * 100),
            "rel60": _r1(None if v("rel60") is None else v("rel60") * 100),
            "volumeRatio20": _num_or_none(v("vol_ratio20"))}


def _timeline(ctx: PublicContext, code: str, shown: dict | None, events_by_id: dict) -> list[dict]:
    out = []
    start = shown["started_at"] if shown else (ctx.days[max(0, len(ctx.days) - 60)])
    for ev in ctx.events:
        if ev["security_code"] != code:
            continue
        label = EVENT_LABEL.get(ev["event_type"], ev["event_type"])
        ratio, prev = ev.get("holding_ratio"), ev.get("previous_holding_ratio")
        if ev["event_type"] == "NEW_HOLDER" and ratio is not None:
            detail = f"保有割合 {ratio:g}%（{ev['filer_name']}）"
        elif ratio is not None and prev is not None:
            detail = f"{prev:g}% → {ratio:g}%（{ev['filer_name']}）"
        else:
            detail = ev["filer_name"]
        out.append({"date": ev["disclosed_at"][:10], "kind": "EVENT", "label": label, "detail": detail,
                    "sourceUrl": ev["source_url"]})
    for ch in ctx.setup_changes:
        if ch["security_code"] != code or ch["date"] < start:
            continue
        if ch["kind"] == "SETUP":
            improved = ch["from"] is None or (ch["from"] == "RECOVERY" and ch["to"] in ("TREND", "BREAKOUT"))
            if ch["to"] is None:
                label, detail = "Price Setup解消", f"{ch['from']} → なし"
            else:
                label = "Price Setup改善" if improved else "Price Setup変化"
                detail = f"{ch['from'] or 'なし'} → {ch['to']}"
        else:
            label, detail = "Price Setup更新", MILESTONE_LABEL.get(ch["milestone"], ch["milestone"])
        out.append({"date": ch["date"], "kind": "SETUP", "label": label, "detail": detail, "sourceUrl": None})
    if shown:
        for h in shown["status_history"]:
            if h["to"] == "CANDIDATE":
                continue
            out.append({"date": h["date"], "kind": "STATUS", "label": h["to"],
                        "detail": REASON_TEXT.get(h["reason_code"], ""), "sourceUrl": None})
    order = {"EVENT": 0, "SETUP": 1, "STATUS": 2}
    out.sort(key=lambda t: (t["date"], order[t["kind"]], t["label"], t["detail"]))
    return out[-ctx.cfg.export.timeline_limit:]


def build_stock(ctx: PublicContext, code: str, events_by_id: dict) -> dict:
    shown = _episode_for(ctx, code)
    past = [e for e in ctx.episodes if e["security_code"] == code and e is not shown and e["status"] == "CLOSED"]
    past.sort(key=lambda e: (e["started_at"], e["episode_id"]))
    episode = None
    if shown:
        episode = {"id": shown["episode_id"], "status": shown["status"], "setupType": shown["setup_type"],
                   "setups": shown["setups"], "watchStartedAt": shown["watch_started_at"],
                   "startedAt": shown["started_at"], "overextended": shown["overextended"],
                   "statusHistory": shown["status_history"],
                   "closedAt": shown["closed_at"], "closeReason": shown["close_reason"]}
    dates, close = ctx.series(code)
    reactions = []
    for ev in ctx.events:
        if ev["security_code"] == code and ev["event_market_date"] <= ctx.as_of:
            reactions.append(performance.event_reaction(ev, dates, close, ctx.bench_close))
    company = {"securityCode": code, "name": ctx.names.get(code, code), "tradingViewSymbol": f"TSE:{code}"}
    return {"schemaVersion": SCHEMA_VERSION, "updatedAt": ctx.now, "company": company, "episode": episode,
            "pastEpisodes": [{"id": e["episode_id"], "startedAt": e["started_at"], "closedAt": e["closed_at"],
                              "closeReason": e["close_reason"], "setupType": e["setup_type"]} for e in past],
            "reasons": _reasons(ctx, shown, events_by_id) if shown else [],
            "timeline": _timeline(ctx, code, shown, events_by_id),
            "priceSetup": _price_setup(ctx, code),
            "margin": ctx.margin_views.get(code),
            "performance": _perf(ctx, shown) if shown else None,
            "eventReactions": sorted(reactions, key=lambda r: r["eventId"])}


def build_public(ctx: PublicContext) -> dict[str, Any]:
    """相対パス → JSON 値。"""
    events_by_id = {e["event_id"]: e for e in ctx.events}
    weekly = [_entry(ctx, e, ctx.weekly.get("previous"), events_by_id) for e in ctx.weekly["entries"]]
    monthly = [_entry(ctx, e, ctx.monthly.get("previous"), events_by_id) for e in ctx.monthly["entries"]]
    changes = _state_changes(ctx)
    summary = {"schemaVersion": SCHEMA_VERSION, "updatedAt": ctx.now, "asOfMarketDate": ctx.as_of,
               "benchmarkLabel": BENCHMARK_LABEL, "sources": ctx.sources,
               "weekly": {"periodKey": ctx.weekly["key"], "entries": weekly},
               "monthly": {"periodKey": ctx.monthly["key"], "entries": monthly},
               "stateChanges": changes, "universeCount": ctx.universe_count,
               "disclaimer": ctx.cfg.export.disclaimer}
    codes = {e["securityCode"] for e in weekly + monthly} | {c["securityCode"] for c in changes} \
        | {e["security_code"] for e in ctx.episodes if e["status"] != "CLOSED"}
    files: dict[str, Any] = {"summary.json": summary}
    for code in sorted(codes):
        files[f"stocks/{code}.json"] = build_stock(ctx, code, events_by_id)
    return files


# --------------------------------------------------------------------------- 検証・置換
def _walk(value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            yield path, k, v
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in ("name", "companyName", "sourceUrl", "tradingViewSymbol"):
                continue
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def validate_public(files: dict[str, Any]) -> None:
    errors = []
    summary = files.get("summary.json")
    if not isinstance(summary, dict) or not SUMMARY_KEYS <= set(summary):
        errors.append("summary.json: required keys missing")
        raise ExportValidationError("; ".join(errors))
    if not summary.get("updatedAt"):
        errors.append("summary.json: updatedAt missing")
    for src in ("edinet", "prices", "jpx400", "margin"):
        if src not in summary["sources"]:
            errors.append(f"summary.sources.{src} missing")
    for kind in ("weekly", "monthly"):
        block = summary[kind]
        if not {"periodKey", "entries"} <= set(block):
            errors.append(f"summary.{kind}: keys missing")
            continue
        for e in block["entries"]:
            if set(e) != ENTRY_KEYS:
                errors.append(f"summary.{kind}: entry keys mismatch {sorted(set(e) ^ ENTRY_KEYS)}")
            if len(e.get("reasons", [])) > MAX_REASONS:
                errors.append(f"{kind}: too many reasons")
            if f"stocks/{e['securityCode']}.json" not in files:
                errors.append(f"{kind}: missing stock file {e['securityCode']}")
    for c in summary["stateChanges"]:
        if f"stocks/{c['securityCode']}.json" not in files:
            errors.append(f"stateChanges: missing stock file {c['securityCode']}")
    for name, obj in files.items():
        if name.startswith("stocks/") and not STOCK_KEYS <= set(obj):
            errors.append(f"{name}: required keys missing")
        for _, key, _ in _walk(obj):
            if any(part in str(key).lower() for part in FORBIDDEN_KEY_PARTS):
                errors.append(f"{name}: forbidden key {key}")
        for text in _strings(obj):
            for word in FORBIDDEN_WORDS:
                if word in text:
                    errors.append(f"{name}: forbidden word {word}")
            if _AI.search(text):
                errors.append(f"{name}: forbidden word AI")
    if errors:
        raise ExportValidationError("; ".join(sorted(set(errors))[:10]))


def write_tree(files: dict[str, Any], root: Path) -> None:
    for rel, obj in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(dumps(obj), encoding="utf-8", newline="\n")


def publish(files: dict[str, Any], out_dir: Path) -> None:
    """一時ディレクトリへ書き出し→読み戻し検証→置換。失敗時は既存を残す。"""
    out_dir = Path(out_dir)
    tmp = out_dir.parent / f".{out_dir.name}.tmp"
    old = out_dir.parent / f".{out_dir.name}.old"
    for p in (tmp, old):
        if p.exists():
            shutil.rmtree(p)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_tree(files, tmp)
        reread = {rel: json.loads((tmp / rel).read_text(encoding="utf-8")) for rel in files}
        validate_public(reread)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if out_dir.exists():
        out_dir.rename(old)
    tmp.rename(out_dir)
    shutil.rmtree(old, ignore_errors=True)
